# executor.py
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from threshold_logic import Threshold
from trade import place_trade, close_symbol_positions
from config import SYMBOL_CONFIGS
_last_action: Dict[str, str] = {}

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _should_fire(symbol: str, action: str) -> bool:
    prev = _last_action.get(symbol)
    if prev != action:
        _last_action[symbol] = action
        return True
    return False

def _safe_trade_response(resp: Any) -> Any:
    try:
        fields = ("retcode", "comment", "order", "deal", "ask", "bid", "price")
        return {k: getattr(resp, k, None) for k in fields}
    except Exception:
        return resp

def execute_place_and_close(
    symbol: str,
    *,
    start: float,
    current: float,
    high: float,
    low: float,
    previous_executables: Optional[Dict[str, Any]] = None,
    dry_run: bool = None,
) -> Dict[str, Any]:
    """
    Run Threshold and (optionally) execute trades.
    Returns a dict with decision, executables, and (if any) trade_response.
    """
    decision = Threshold(symbol, start, current, high, low).run(previous_executables=previous_executables)
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
    lot_size = symbol_data.lot_size
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
        "executables": execs,            # <- return so caller can persist timestamps
        "trade_response": None,
        "note": None,
    }

    if action == "wait":
        print("→ no action (wait)")
        result["note"] = "no-op"
        return result

    if not _should_fire(symbol, action):
        print("→ duplicate action suppressed")
        result["note"] = "duplicate_action_suppressed"
        return result

    if dry_run:
        print("→ DRY RUN mode (no MT5 call)")
        result["note"] = "dry_run"
        return result

    try:
        if action == "place_long_trade":
            print(f"→ placing LONG (BUY) for {symbol}")
            resp = place_trade(symbol, "buy", symbol_data.lot_size)
            result["trade_response"] = _safe_trade_response(resp)

        elif action == "place_short_trade":
            print(f"→ placing SHORT (SELL) for {symbol}")
            resp = place_trade(symbol, "sell", symbol_data.lot_size)
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


# # --- SAMPLE RUN ---
# if __name__ == "__main__":
#     """
#     Simulated sample:
#     - start = 3999.00 (anchor)
#     - current = 4002.00 (movement)
#     - high / low boundaries simulate intraday levels
#     """
#     print("\n--- Sample Run: Threshold Execution Demo ---")
#
#     # You can toggle dry_run=True to skip MT5 trades
#     response = execute_place_and_close(
#         symbol="XAUUSD",
#         start=3999.00,
#         current=3996.00,
#         high=4003.77,
#         low=4000.76,
#         dry_run=True
#     )
#
#     print("\n=== Final Response ===")
#     for k, v in response.items():
#         print(f"{k}: {v}")
