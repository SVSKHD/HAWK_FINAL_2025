# executor.py
from __future__ import annotations

from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timezone

import MetaTrader5 as mt5

from threshold_logic import Threshold
from trade import place_trade, close_symbol_positions, close_all_trades
from config import SYMBOL_CONFIGS
from mongo_connector import STATE as mongo_state

# ---- CONFIG ----
PROFIT_LIMIT_USD = 300.0  # daily global "enough" limit

_last_action: Dict[str, str] = {}   # suppress repeated actions per symbol
_halted_for_today: bool = False     # once true, no more new entries
_init_halt_checked: bool = False    # load halt state from Mongo once


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _should_fire(symbol: str, action: str) -> bool:
    """
    Avoid spamming the same action in the tight loop for the same symbol.
    Example: place_long_trade every second once threshold is hit.
    """
    prev = _last_action.get(symbol)
    if prev != action:
        _last_action[symbol] = action
        return True
    return False


def _safe_trade_response(resp: Any) -> Any:
    """
    Normalize MT5 response into a simple dict so logs / Discord don't break.
    """
    try:
        fields = ("retcode", "comment", "order", "deal", "ask", "bid", "price")
        return {k: getattr(resp, k, None) for k in fields}
    except Exception:
        return resp


# ---------------------- PnL helpers ----------------------

def _today_utc_bounds() -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


def get_today_realized_profit() -> float:
    utc_from, utc_to = _today_utc_bounds()
    deals = mt5.history_deals_get(utc_from, utc_to)
    if not deals:
        return 0.0
    return float(sum(float(getattr(d, "profit", 0.0)) for d in deals))


def get_open_pnl() -> float:
    positions = mt5.positions_get()
    if not positions:
        return 0.0
    return float(sum(float(getattr(p, "profit", 0.0)) for p in positions))


def get_total_pnl() -> float:
    return get_today_realized_profit() + get_open_pnl()


def _check_profit_watchdog() -> Tuple[bool, float]:
    total = get_total_pnl()
    return total >= PROFIT_LIMIT_USD, total


# ---------------------- Halt state restore ----------------------

def _ensure_halt_state_loaded() -> None:
    """
    Lazy load halt state from Mongo on first executor call.
    If bot restarts and we halted earlier today, keep entries OFF.
    """
    global _init_halt_checked, _halted_for_today
    if _init_halt_checked:
        return
    _init_halt_checked = True

    doc = mongo_state.get_profit_halt_today()
    if doc and doc.get("halted"):
        _halted_for_today = True
        print(
            f"[executor] Trading already halted for today "
            f"(total_pnl_at_halt={doc.get('total_pnl')})."
        )
    else:
        print("[executor] No halt recorded for today; entries allowed.")


# -------------------------- MAIN --------------------------

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
    Flow:
      1. Ensure halt state synced from Mongo (after restart).
      2. If previous_executables is None → restore threshold timestamps from Mongo.
      3. Run Threshold and get executables (action, direction, first/second threshold).
      4. Upsert latest threshold timestamps to Mongo.
      5. Check profit watchdog (account-level PnL).
         - On first cross of PROFIT_LIMIT_USD today:
             * Record halt in Mongo
             * Close all trades (if not dry_run)
             * Set _halted_for_today = True
      6. If halted_for_today:
         - Block new entries (place_long / place_short)
         - Allow 'close' actions
      7. De-dup action per symbol.
      8. If dry_run: only log & return.
      9. Otherwise execute MT5 trade and return response.
    """

    global _halted_for_today

    # 1) Load halt state once
    _ensure_halt_state_loaded()

    # 2) Restore threshold timestamps if we don't have a memory state yet
    if previous_executables is None:
        restored = mongo_state.load_threshold_state(symbol)
        previous_executables = {
            "first_threshold_reached_at": restored["first_threshold_reached_at"],
            "second_threshold_reached_at": restored["second_threshold_reached_at"],
        }

    # 3) Threshold decision
    decision = Threshold(symbol, start, current, high, low).run(
        previous_executables=previous_executables
    )
    execs = decision["executables"]
    action = execs["action"]

    print("\n=== Decision ===")
    print(f"symbol: {symbol}")
    print(f"action: {action}")
    print(f"direction: {execs['direction']}")
    print(f"threshold_scale: {decision['threshold_scale']} (abs={decision['abs_threshold_scale']})")
    print(f"first@1x: {execs.get('first_threshold_reached_at')}")
    print(f"second@2x: {execs.get('second_threshold_reached_at')}")
    print(f"breach_high: {execs['breach_high']}  breach_low: {execs['breach_low']}")

    symbol_data = SYMBOL_CONFIGS.get(symbol)
    lot_size = symbol_data.lot_size if symbol_data else 0.01

    # 4) Persist threshold state in Mongo
    try:
        mongo_state.upsert_threshold_state(
            symbol,
            first_threshold_reached_at=execs.get("first_threshold_reached_at"),
            second_threshold_reached_at=execs.get("second_threshold_reached_at"),
        )
    except Exception as e:
        print(f"[WARN] failed to upsert threshold state for {symbol}: {e}")

    # 5) Profit watchdog
    limit_reached_now, total_pnl = _check_profit_watchdog()

    if limit_reached_now and not _halted_for_today:
        _halted_for_today = True
        print(f"🚨 PROFIT LIMIT HIT: total_pnl={total_pnl:.2f} >= {PROFIT_LIMIT_USD:.2f}")

        try:
            mongo_state.set_profit_halt(
                total_pnl=total_pnl,
                limit_usd=PROFIT_LIMIT_USD,
                note="auto-halt from executor",
            )
        except Exception as e:
            print(f"[WARN] failed to persist halt state: {e}")

        if not dry_run:
            try:
                print("→ Closing ALL open positions due to profit limit.")
                close_all_trades()
            except Exception as e:
                print(f"[ERROR] close_all_trades failed: {e}")

    result: Dict[str, Any] = {
        "timestamp": _now_iso(),
        "symbol": symbol,
        "action": action,
        "direction": execs["direction"],
        "start": decision["start"],
        "current": decision["current"],
        "high": decision["high"],
        "low": decision["low"],
        "threshold_scale": decision["threshold_scale"],
        "executables": execs,
        "trade_response": None,
        "note": None,
        "total_pnl": total_pnl,
        "profit_limit_reached": _halted_for_today,
    }

    # 6) Wait action
    if action == "wait":
        print("→ no action (wait)")
        result["note"] = "no-op"
        return result

    # 7) If halted → block new entries, but allow 'close'
    if _halted_for_today and action in ("place_long_trade", "place_short_trade"):
        print("→ Trading HALTED for today; blocking new entry.")
        result["note"] = "blocked_by_profit_limit"
        return result

    # 8) De-dup repeated same action
    if not _should_fire(symbol, action):
        print("→ duplicate action suppressed")
        result["note"] = "duplicate_action_suppressed"
        return result

    # 9) Dry run
    if dry_run:
        print("→ DRY RUN mode (no MT5 call)")
        result["note"] = "dry_run"
        return result

    # 10) Real trade execution
    try:
        if action == "place_long_trade":
            print(f"→ placing LONG (BUY) for {symbol}, lot={lot_size}")
            resp = place_trade(symbol, "buy", lot_size)
            result["trade_response"] = _safe_trade_response(resp)

        elif action == "place_short_trade":
            print(f"→ placing SHORT (SELL) for {symbol}, lot={lot_size}")
            resp = place_trade(symbol, "sell", lot_size)
            result["trade_response"] = _safe_trade_response(resp)

        elif action == "close":
            print(f"→ closing positions for {symbol}")
            resp = close_symbol_positions(symbol)
            result["trade_response"] = _safe_trade_response(resp)

        else:
            print(f"→ unknown action: {action}")
            result["note"] = f"unknown action {action}"

    except Exception as e:
        result["note"] = f"execution error: {e}"
        print(f"[ERROR] execution: {e}")

    return result
