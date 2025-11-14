from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Optional, Dict, Any
from datetime import datetime, timezone
from common_logic import PriceComponent
from config import SYMBOL_CONFIGS
from trade import place_trade, close_symbol_positions
from notify import send_discord_message
from mt5 import init_mt5


Action = Literal["PLACE", "HOLD", "CLOSE", "NONE"]
Side = Literal["LONG", "SHORT"]

@dataclass
class TradeDetails:
    placed_symbol: str
    direction_trade_placed: Optional[Side] = None
    time_of_placement: Optional[str] = None  # ISO time
    time_of_exit: Optional[str] = None       # ISO time
    lot_size: Optional[float] = None
    trade_id: Optional[str] = None
    strategy_tag: str = "ThresholdV1"

@dataclass
class ThresholdDecision:
    signal: Action
    reason: str
    metrics: Dict[str, Any]
    trade_details: TradeDetails


# Windows for action
PLACE_MIN = 1.00
PLACE_MAX = 1.25
CLOSE_MIN = 1.80
CLOSE_MAX = 2.00


def _infer_side(pc: PriceComponent) -> Side:
    # Prefer strong_direction if available, else immediate direction
    dirn = pc.strong_direction if pc.strong_direction != "FLAT" else pc.direction
    return "LONG" if dirn == "UP" else "SHORT"


def _fmt_place_lines(decision: 'ThresholdDecision') -> str:
    m = decision.metrics or {}
    symbol = m.get("symbol") or decision.trade_details.placed_symbol
    side = decision.trade_details.direction_trade_placed or m.get("suggested_side")
    ratio = m.get("threshold_ratio") or m.get("ratio") or 0
    start_price = m.get("start_price")
    current_price = m.get("current_price")
    latest_high = m.get("latest_high")
    latest_low = m.get("latest_low")
    pips_moved = m.get("pips_moved")
    threshold_pips = m.get("threshold_pips")

    lines = [
        "TRADE SIGNAL: PLACE (1st threshold)",
        f"Symbol: {symbol}",
        f"Side: {side}",
        f"Threshold Ratio: {ratio:.2f}",
        f"Pips Moved: {pips_moved}",
        f"Threshold (pips): {threshold_pips}",
        f"Start Price: {start_price}",
        f"Current Price: {current_price}",
        f"Latest High: {latest_high}",
        f"Latest Low: {latest_low}",
        "Strategy: ThresholdV1",
    ]
    return "\n".join(str(x) for x in lines if x is not None)


def _fmt_close_lines(decision: 'ThresholdDecision') -> str:
    m = decision.metrics or {}
    symbol = m.get("symbol") or decision.trade_details.placed_symbol
    ratio = m.get("threshold_ratio") or m.get("ratio") or 0
    start_price = m.get("start_price")
    current_price = m.get("current_price")
    pips_moved = m.get("pips_moved")
    threshold_pips = m.get("threshold_pips")

    lines = [
        "TRADE SIGNAL: CLOSE (2nd threshold)",
        f"Symbol: {symbol}",
        f"Threshold Ratio: {ratio:.2f}",
        f"Pips Moved: {pips_moved}",
        f"Threshold (pips): {threshold_pips}",
        f"Start Price: {start_price}",
        f"Current Price: {current_price}",
        "Strategy: ThresholdV1",
    ]
    return "\n".join(str(x) for x in lines if x is not None)


def evaluate_threshold(
    pc: PriceComponent,
    is_position_open: bool,
    now: Optional[datetime] = None,
) -> ThresholdDecision:

    now = now or datetime.now(timezone.utc)
    ratio = pc.threshold_ratio  # already abs distance / threshold_pips
    side = _infer_side(pc)

    cfg = SYMBOL_CONFIGS[pc.symbol]
    td = TradeDetails(
        placed_symbol=pc.symbol,
        direction_trade_placed=None,
        time_of_placement=None,
        time_of_exit=None,
        lot_size=cfg.lot_size,
        strategy_tag="ThresholdV1",
    )

    # metrics include pc dict plus windows and direction
    metrics = {
        **pc.as_dict(),                # expect keys like: symbol, start_price, current_price, latest_high, latest_low, pips_moved, threshold_pips, threshold_ratio, etc.
        "ratio": ratio,                # safe duplicate
        "window_place": [PLACE_MIN, PLACE_MAX],
        "window_close": [CLOSE_MIN, CLOSE_MAX],
        "suggested_side": side,
    }

    # Close logic applies only if already open
    if is_position_open and CLOSE_MIN <= ratio <= CLOSE_MAX:
        td.direction_trade_placed = side  # direction of the open position (for logging/consistency)
        td.time_of_exit = now.isoformat()
        decision = ThresholdDecision(
            signal="CLOSE",
            reason=f"threshold_ratio {ratio:.2f} in close window [{CLOSE_MIN}, {CLOSE_MAX}]",
            metrics=metrics,
            trade_details=td,
        )
        return decision

    # Place logic applies only if not already open
    if not is_position_open and PLACE_MIN <= ratio <= PLACE_MAX:
        td.direction_trade_placed = side
        td.time_of_placement = now.isoformat()
        decision = ThresholdDecision(
            signal="PLACE",
            reason=f"threshold_ratio {ratio:.2f} in place window [{PLACE_MIN}, {PLACE_MAX}]",
            metrics=metrics,
            trade_details=td,
        )
        return decision

    # If a position is open but we’re not in close window → HOLD
    if is_position_open:
        return ThresholdDecision(
            signal="HOLD",
            reason=f"position open; threshold_ratio {ratio:.2f} not in close window",
            metrics=metrics,
            trade_details=td,
        )

    # Nothing to do if no position open and not in place window
    return ThresholdDecision(
        signal="NONE",
        reason=f"no position; threshold_ratio {ratio:.2f} not in place window",
        metrics=metrics,
        trade_details=td,
    )


def _is_symbol_open(symbol: str) -> bool:
    """Tiny helper to detect if symbol has any open positions."""
    try:
        import MetaTrader5 as mt5  # local import to avoid hard dep at import-time
        positions = mt5.positions_get(symbol=symbol) or ()
        return len(positions) > 0
    except Exception:
        return False


def execute_threshold_decision(decision: ThresholdDecision) -> Dict[str, Any]:
    symbol = decision.trade_details.placed_symbol
    cfg = SYMBOL_CONFIGS[symbol]

    if not getattr(cfg, "is_trade_able", True):
        return {"executed": False, "message": f"{symbol} not tradeable", "result": None}

    init_mt5("threshold_logic.py")

    if decision.signal == "PLACE":
        side = decision.trade_details.direction_trade_placed  # LONG or SHORT

        # Notify about the signal itself (never block trading on notify issues)
        try:
            send_discord_message("info", _fmt_place_lines(decision))
        except Exception:
            pass

        # 🔁 Map LONG/SHORT -> buy/sell for trade.place_trade API
        trade_type = "buy" if side == "LONG" else "sell"

        # Call your trade layer
        res = place_trade(
            symbol=symbol,
            trade_type=trade_type,           # <-- mapped
            volume=cfg.lot_size,
            comment=f"ThresholdV1 place {side}",
        )

        # res is the normalized dict from place_trade()
        ok = bool(res.get("ok")) if isinstance(res, dict) else False
        return {"executed": ok, "message": "PLACE sent", "result": res}

    if decision.signal == "CLOSE":
        # Notify intent before attempting broker call
        try:
            send_discord_message("info", _fmt_close_lines(decision))
        except Exception:
            pass

        res_list = close_symbol_positions(symbol, deviation=10)
        ok = len(res_list) > 0
        return {"executed": ok, "message": "CLOSE sent", "result": res_list}

    return {"executed": False, "message": f"No trade for signal={decision.signal}", "result": None}

# --- quick demo / manual test ---
# if __name__ == "__main__":
#     pc = PriceComponent(
#         symbol="GBPUSD",
#         start_price=1.34734,
#         current_price=1.34142,
#         latest_high=1.34895,
#         latest_low=1.34154,
#     )
#     pos_open = _is_symbol_open(pc.symbol)
#     d1 = evaluate_threshold(pc, is_position_open=pos_open)
#     print("Decision:", d1.signal, d1.reason)
#     exec_res = execute_threshold_decision(d1)
#     print("Exec:", exec_res["message"], exec_res["result"])
