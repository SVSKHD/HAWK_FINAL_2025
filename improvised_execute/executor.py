# executor.py

from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timezone

import metatrader5 as mt5

from threshold_logic import Threshold
from trade import place_trade, close_symbol_positions
from config import SYMBOL_CONFIGS, IST, HOUR, MINUTES, WATCHDOG_FROM_UTC
from mongo_connector import STATE as mongo_state  # <- MongoState singleton


# ==========================
#   CONFIG
# ==========================

# When total PnL (realized today + open PnL) >= this,
# we will:
#   1) Close positions for this symbol (once)
#   2) Lock the day (no more new entries)
PROFIT_LIMIT_USD = 300.0

# Optional safety: if account drops below this (loss),
# we can also lock the day. Set to None to disable.
LOSS_LIMIT_USD: Optional[float] = None  # e.g. -200.0


# In-memory last action per symbol to avoid spamming same command
_last_action: Dict[str, str] = {}
ASTRA_PREFIX = "Astra-"   # must match trade.py comment prefix


# ==========================
#   SMALL HELPERS
# ==========================

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _should_fire(symbol: str, action: str) -> bool:
    """
    Avoid sending the *same* action every tick for the same symbol.
    Example: threshold says 'place_long_trade' for 20 seconds straight.
    """
    prev = _last_action.get(symbol)
    if prev != action:
        _last_action[symbol] = action
        return True
    return False


def _safe_trade_response(resp: Any) -> Any:
    """
    Normalize MT5 response into a simple dict so logs / Discord won't break.
    """
    try:
        fields = ("retcode", "comment", "order", "deal", "ask", "bid", "price")
        return {k: getattr(resp, k, None) for k in fields}
    except Exception:
        return resp


# ==========================
#   PNL WATCHDOG (ACCOUNT)
# ==========================

def _profit_window_bounds() -> tuple[datetime, datetime]:
    now = _now_utc()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)  # or from Mongo anchor later
    return start, now

def get_window_realized_profit() -> float:
    """
    Realized profit only for Astra trades in current window.
    """
    utc_from, utc_to = _profit_window_bounds()
    deals = mt5.history_deals_get(utc_from, utc_to)
    if not deals:
        return 0.0

    total = 0.0
    for d in deals:
        comment = (getattr(d, "comment", "") or "").strip()
        if not comment.startswith(ASTRA_PREFIX):
            continue
        total += float(getattr(d, "profit", 0.0))
    return total

def get_total_pnl() -> float:
    # WATCHDOG = realized Astra profit only
    return get_window_realized_profit()


# ==========================
#   MONGO DAY STATE HELPERS
# ==========================

def _get_day_state() -> Dict[str, Any]:
    """
    Get today's trading state from Mongo.
    Expected mongo_connector.MongoState interface:
      - get_today_state() -> dict (creates if not exists)
    """
    try:
        return mongo_state.get_today_state()
    except Exception as e:
        print(f"[WARN] mongo_state.get_today_state failed: {e}")
        # Fallback in-memory minimal state
        return {"locked": False, "lock_reason": None, "max_total_pnl": 0.0}


def _update_day_state(patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    Update today's trading state in Mongo with given fields.
    Returns updated state.
    """
    try:
        return mongo_state.update_today_state(patch)
    except Exception as e:
        print(f"[WARN] mongo_state.update_today_state failed: {e}")
        # best-effort fallback: just return patch merged to bare dict
        base = {"locked": False, "lock_reason": None, "max_total_pnl": 0.0}
        base.update(patch)
        return base


def _append_trade_event(event: Dict[str, Any]) -> None:
    """
    Append a per-event log (for audit / analysis).
    """
    try:
        mongo_state.append_trade_event(event)
    except Exception as e:
        print(f"[WARN] mongo_state.append_trade_event failed: {e}")


def _check_and_update_watchdog() -> Dict[str, Any]:
    """
    Compute current total PnL and enforce daily lock.

    Returns a dict:
      {
        "total_pnl": float,
        "locked": bool,
        "lock_reason": Optional[str],
        "just_locked": bool,
    }
    """
    total_pnl = get_total_pnl()
    state = _get_day_state()

    locked = bool(state.get("locked", False))
    lock_reason = state.get("lock_reason")
    max_total_pnl = float(state.get("max_total_pnl", 0.0))

    just_locked = False

    # Always track max PnL seen today
    if total_pnl > max_total_pnl:
        state = _update_day_state({"max_total_pnl": total_pnl})
        max_total_pnl = total_pnl

    # If already locked, we do not unlock even if pnl drops later
    if locked:
        return {
            "total_pnl": total_pnl,
            "locked": True,
            "lock_reason": lock_reason,
            "just_locked": False,
        }

    # --- Profit limit lock ---
    if PROFIT_LIMIT_USD is not None and total_pnl >= PROFIT_LIMIT_USD:
        locked = True
        lock_reason = f"profit_limit_reached_{total_pnl:.2f}"
        state = _update_day_state({"locked": True, "lock_reason": lock_reason})
        just_locked = True

    # --- Loss limit lock (optional) ---
    if (not locked) and LOSS_LIMIT_USD is not None and total_pnl <= LOSS_LIMIT_USD:
        locked = True
        lock_reason = f"loss_limit_reached_{total_pnl:.2f}"
        state = _update_day_state({"locked": True, "lock_reason": lock_reason})
        just_locked = True

    return {
        "total_pnl": total_pnl,
        "locked": locked,
        "lock_reason": lock_reason,
        "just_locked": just_locked,
    }


# ==========================
#   MAIN EXECUTOR
# ==========================

def execute_place_and_close(
    symbol: str,
    *,
    start: float,
    current: float,
    high: float,
    low: float,
    previous_executables: Optional[Dict[str, Any]] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Run Threshold and (optionally) execute trades.

    FLOW:
      1. Compute threshold decision (Threshold.run).
      2. Query account-level PnL and Mongo day_state → watchdog.
      3. If day is locked:
             - Block new entries
             - Still allow 'close' if Threshold says so.
      4. De-duplicate actions per symbol (no spam).
      5. Respect dry_run flag.
      6. Execute place_trade / close_symbol_positions.
      7. Log event to Mongo.
    """
    # --- 1) Threshold decision ---
    decision = Threshold(symbol, start, current, high, low).run(
        previous_executables=previous_executables
    )
    execs = decision["executables"]
    action = execs["action"]
    direction = execs["direction"]

    print("\n=== Decision ===")
    print(f"symbol: {symbol}")
    print(f"action: {action}")
    print(f"direction: {direction}")
    print(f"threshold_scale: {decision['threshold_scale']} (abs={decision['abs_threshold_scale']})")
    print(f"first@1x: {execs.get('first_threshold_reached_at')}")
    print(f"second@2x: {execs.get('second_threshold_reached_at')}")
    print(f"breach_high: {execs['breach_high']}  breach_low: {execs['breach_low']}")

    symbol_data = SYMBOL_CONFIGS.get(symbol)
    lot_size = symbol_data.lot_size if symbol_data else 0.01

    # --- 2) Profit watchdog & daily lock ---
    watchdog = _check_and_update_watchdog()
    total_pnl = watchdog["total_pnl"]
    locked = watchdog["locked"]
    lock_reason = watchdog["lock_reason"]
    just_locked = watchdog["just_locked"]

    result: Dict[str, Any] = {
        "timestamp": _now_iso(),
        "symbol": symbol,
        "action": action,
        "direction": direction,
        "start": decision["start"],
        "current": decision["current"],
        "high": decision["high"],
        "low": decision["low"],
        "threshold_scale": decision["threshold_scale"],
        "executables": execs,            # returned so main_runner can persist timestamps
        "trade_response": None,
        "note": None,
        "total_pnl": total_pnl,
        "profit_limit_reached": locked and lock_reason and "profit_limit" in lock_reason,
        "loss_limit_reached": locked and lock_reason and "loss_limit" in lock_reason,
    }

    # --- 3) If watchdog just locked, close positions for this symbol once (if live) ---
    if just_locked and not dry_run:
        print(f"→ WATCHDOG LOCK triggered: {lock_reason}")
        print(f"→ closing all positions for {symbol} due to PnL limit.")
        try:
            resp = close_symbol_positions(symbol)
            result["trade_response"] = _safe_trade_response(resp)
            result["note"] = "watchdog_lock_close"
        except Exception as e:
            print(f"[ERROR] close_symbol_positions during watchdog lock: {e}")
            result["note"] = f"watchdog_close_error: {e}"

        # Log the watchdog event
        _append_trade_event({
            "ts": result["timestamp"],
            "symbol": symbol,
            "event": "watchdog_lock",
            "total_pnl": total_pnl,
            "lock_reason": lock_reason,
        })

        # Even if Threshold says place_long/short, we do not proceed further in this tick
        return result

    # --- 4) If day is locked, block NEW ENTRIES but allow 'close' ---
    if locked and action in ("place_long_trade", "place_short_trade"):
        print(f"→ DAY LOCKED ({lock_reason}). Blocking new entry for {symbol}.")
        result["note"] = "blocked_by_daily_lock"
        _append_trade_event({
            "ts": result["timestamp"],
            "symbol": symbol,
            "event": "blocked_by_daily_lock",
            "action": action,
            "total_pnl": total_pnl,
            "lock_reason": lock_reason,
        })
        return result

    # --- 5) Ignore 'wait' actions (no-op) ---
    if action == "wait":
        print("→ no action (wait)")
        result["note"] = "no-op"
        _append_trade_event({
            "ts": result["timestamp"],
            "symbol": symbol,
            "event": "wait",
            "total_pnl": total_pnl,
        })
        return result

    # --- 6) De-dup actions per symbol (avoid spamming) ---
    if not _should_fire(symbol, action):
        print("→ duplicate action suppressed")
        result["note"] = "duplicate_action_suppressed"
        _append_trade_event({
            "ts": result["timestamp"],
            "symbol": symbol,
            "event": "duplicate_action_suppressed",
            "action": action,
            "total_pnl": total_pnl,
        })
        return result

    # --- 7) DRY-RUN mode (no MT5 trades) ---
    if dry_run:
        print("→ DRY RUN mode (no MT5 call)")
        result["note"] = "dry_run"
        _append_trade_event({
            "ts": result["timestamp"],
            "symbol": symbol,
            "event": "dry_run_action",
            "action": action,
            "direction": direction,
            "total_pnl": total_pnl,
        })
        return result

    # --- 8) REAL TRADE EXECUTION ---
    try:
        if action == "place_long_trade":
            print(f"→ placing LONG (BUY) for {symbol}, lot={lot_size}")
            resp = place_trade(symbol, "buy", lot_size)
            result["trade_response"] = _safe_trade_response(resp)
            result["note"] = "placed_long"

        elif action == "place_short_trade":
            print(f"→ placing SHORT (SELL) for {symbol}, lot={lot_size}")
            resp = place_trade(symbol, "sell", lot_size)
            result["trade_response"] = _safe_trade_response(resp)
            result["note"] = "placed_short"

        elif action == "close":
            print(f"→ closing positions for {symbol}")
            resp = close_symbol_positions(symbol)
            result["trade_response"] = _safe_trade_response(resp)
            result["note"] = "closed_positions"

        else:
            print(f"→ unknown action: {action}")
            result["note"] = f"unknown_action_{action}"

    except Exception as e:
        result["note"] = f"execution_error: {e}"
        print(f"[ERROR] execution: {e}")

    # --- 9) Log event to Mongo ---
    _append_trade_event({
        "ts": result["timestamp"],
        "symbol": symbol,
        "event": result["note"],
        "action": action,
        "direction": direction,
        "total_pnl": total_pnl,
        "trade_response": result["trade_response"],
    })

    return result


# ==========================
#   SAMPLE DRY-RUN TEST
# ==========================
if __name__ == "__main__":
    print("\n--- Sample Run: Threshold Execution Demo (dry_run) ---")

    resp = execute_place_and_close(
        symbol="XAUUSD",
        start=3999.00,
        current=4002.00,
        high=4005.00,
        low=3990.00,
        previous_executables=None,
        dry_run=True,  # 🚫 no live trades, only decision + watchdog state
    )

    print("\n=== Final Response ===")
    for k, v in resp.items():
        print(f"{k}: {v}")
