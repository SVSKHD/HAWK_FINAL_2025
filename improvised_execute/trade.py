from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Literal

import metatrader5 as mt5

from notify import send_discord_message
from mt5 import init_mt5
from utils import format_discord_trade_message, _format_failure, _format_success, normalize_trade_result

# Type alias for clarity
TradeType = Literal["buy", "sell"]

# --- Comment + ASCII constraints ---
MAX_COMMENT_LEN = 16  # be extra conservative
_ascii_re = re.compile(r"[^A-Za-z0-9\-]")  # only letters, digits, hyphen


# ============================================================
#   COMMENT HELPERS (ASTRA DAILY TAGS)
# ============================================================

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


import re
from datetime import datetime, timezone




def make_order_comment(base: Optional[str] = None) -> str:
    """
    Build a *very* broker-safe order comment:
    - uppercase letters, digits, hyphen only
    - no spaces
    - max 16 characters
    Example: ASTB-1711251305
    """
    now = datetime.now(timezone.utc)
    # short bot tag + time: AST(B/S) + - + DDMM + HHMM
    # e.g. ASTB-1711-1305  -> strip non-allowed and trim
    base = (base or "AST").upper()
    base = _ascii_re.sub("", base)
    ts = now.strftime("%d%m%H%M")  # DDMMHHMM

    raw = f"{base}-{ts}"  # ex: ASTB-17111305
    raw = _ascii_re.sub("", raw)
    return raw[:MAX_COMMENT_LEN]



def make_astra_base(trade_type: TradeType) -> str:
    """
    Base pattern used for Astra trades in this bot:

      Astra-DDMMYY-BUY
      Astra-DDMMYY-SELL

    This base is then passed into make_order_comment(), so the final
    order comment will *start* with Astra-DDMMYY-BUY/SELL and then
    may have truncated timestamp.
    """
    day_tag = _now_utc().strftime("%d%m%y")  # e.g. 171125
    base = f"Astra-{day_tag}-{trade_type.upper()}"
    base = base.replace("–", "-").replace("—", "-")
    base = _ascii_re.sub("", base)
    return base[:MAX_COMMENT_LEN]


def today_astra_prefix() -> str:
    """
    Prefix that identifies today's Astra trades for filtering:

      Astra-DDMMYY-
    """
    day_tag = _now_utc().strftime("%d%m%y")
    return f"Astra-{day_tag}-"


# ============================================================
#   MT5 / POSITION HELPERS
# ============================================================

def _ensure_mt5() -> None:
    """
    Ensure MT5 is connected via your wrapper. Raises if not.
    """
    # Use your own helper first
    init_mt5("called from trade.py")
    # (init_mt5 should internally initialize MT5; if not, you can also check mt5.initialize() here)


def get_open_positions(symbol: Optional[str] = None) -> List[Any]:
    """
    Return list of open positions. If symbol is given, filter to that symbol.
    """
    if symbol:
        positions = mt5.positions_get(symbol=symbol)
    else:
        positions = mt5.positions_get()

    if positions is None:
        return []
    return list(positions)


def get_astra_positions_today(symbol: Optional[str] = None) -> List[Any]:
    """
    Return all *Astra* open positions for *today* (by comment prefix Astra-DDMMYY-).
    Optional: filter by symbol.
    """
    prefix = today_astra_prefix()
    all_pos = get_open_positions(symbol)
    return [p for p in all_pos if getattr(p, "comment", "").startswith(prefix)]


def has_conflicting_position(symbol: str, trade_type: TradeType) -> bool:
    """
    Check if there is an open position in the *opposite* direction for this symbol.
    This prevents simultaneous BUY + SELL on the same symbol.

    - Allows multiple stacked same-direction positions (e.g. multiple BUYs).
    - Blocks BUY if any SELL is open.
    - Blocks SELL if any BUY is open.
    """
    positions = get_open_positions(symbol)
    if not positions:
        return False

    for p in positions:
        p_type = getattr(p, "type", None)
        if trade_type == "buy" and p_type == mt5.POSITION_TYPE_SELL:
            return True
        if trade_type == "sell" and p_type == mt5.POSITION_TYPE_BUY:
            return True
    return False


# ============================================================
#   TRADE EXECUTION
# ============================================================

def place_trade(symbol: str, trade_type: TradeType, volume: float, comment: Optional[str] = None):
    """
    Place a market order (BUY/SELL) for symbol with Astra daily comment.

    - Uses `make_astra_base()` to produce comments like:
        Astra-171125-BUY ...
    - Prevents simultaneous opposite-side positions on the same symbol.
    """
    _ensure_mt5()

    # 1) Guard: prevent conflicting opposite position
    if has_conflicting_position(symbol, trade_type):
        msg = (
            f"⚠️ Trade skipped (conflicting position exists)\n"
            f"**Symbol:** {symbol}\n"
            f"**Requested:** {trade_type.upper()} {volume}\n"
        )
        print(msg)
        send_discord_message("alert", msg)
        return False

    # 2) Fetch tick
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        msg = f"❌ No tick data for {symbol}"
        print(msg)
        send_discord_message("critical", msg)
        return False

    is_buy = (trade_type == "buy")
    price = tick.ask if is_buy else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL

    # 3) Comment (Astra-DDMMYY-BUY/SELL + timestamp)
    base = comment or make_astra_base(trade_type)
    order_comment = make_order_comment(base)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": float(price),
        "deviation": 10,
        "comment": order_comment,
        "type_filling": mt5.ORDER_FILLING_FOK,
        "type_time": mt5.ORDER_TIME_GTC,
    }

    result = mt5.order_send(request)
    print(result)

    if result is None:
        msg = (
            "⚠️ **Trade Failed (no response)**\n"
            f"**Symbol:** {symbol}\n**Type:** {trade_type.upper()}\n"
            f"**Volume:** {volume}\n**Price (attempted):** {price}"
        )
        print(msg)
        send_discord_message("critical", msg)
        return False

    if int(getattr(result, "retcode", 0)) != mt5.TRADE_RETCODE_DONE:
        msg = _format_failure(symbol, trade_type, volume, price, result)
        print(msg)
        send_discord_message("critical", msg)
        return False

    msg = _format_success(symbol, trade_type, volume, price, result)
    print(msg)
    send_discord_message("info", msg)
    return result


# ============================================================
#   CLOSE ALL TRADES (DANGEROUS UTILITY)
# ============================================================

def close_all_trades(
    *,
    deviation: int = 10,
    include_masks: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Close ALL open positions, optionally filtering by symbol substrings.

    ⚠ Warning:
        This is a heavy hammer. Use mainly for emergency/manual cleanup.

    - include_masks: list of substrings; position is included if any mask
      is present in pos.symbol (case-insensitive).
    """
    results: List[Dict[str, Any]] = []

    _ensure_mt5()

    positions = mt5.positions_get()
    if positions is None:
        code, details = mt5.last_error()
        print(f"[positions_get=None] last_error={code} {details}")
        return results

    if len(positions) == 0:
        print("[close_all_trades] No open positions.")
        return results

    def _keep(pos) -> bool:
        if not include_masks:
            return True
        sym = getattr(pos, "symbol", "")
        return any(m.lower() in sym.lower() for m in include_masks)

    filtered = [p for p in positions if _keep(p)]
    if not filtered:
        print(f"[close_all_trades] No positions matched include_masks={include_masks}")
        return results

    for pos in filtered:
        symbol = pos.symbol
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            code, details = mt5.last_error()
            print(f"[skip] No tick for {symbol}. last_error={code} {details}")
            continue

        is_long = (pos.type == mt5.POSITION_TYPE_BUY)
        close_type = mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY
        price = tick.bid if is_long else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "type": close_type,
            "position": int(getattr(pos, "ticket", 0)),
            "price": float(price),
            "volume": float(pos.volume),
            "deviation": int(deviation),
            "type_filling": mt5.ORDER_FILLING_FOK,
            "type_time": mt5.ORDER_TIME_GTC,
            "comment": f"bot-2025 close {'long' if is_long else 'short'}",
        }

        res = mt5.order_send(request)
        rec = {
            "symbol": symbol,
            "position_ticket": int(getattr(pos, "ticket", 0)),
            "closed_side": "SELL" if is_long else "BUY",
            "volume": float(pos.volume),
            "price": float(price),
            "request": request,
            "response": {
                "retcode": getattr(res, "retcode", None),
                "comment": getattr(res, "comment", None),
                "order": getattr(res, "order", None),
                "deal": getattr(res, "deal", None),
            },
        }
        print(
            f"[closed] {symbol} {rec['closed_side']} "
            f"vol={rec['volume']} price={rec['price']} "
            f"ret={rec['response']['retcode']}"
        )
        results.append(rec)

    return results


# ============================================================
#   CLOSE POSITIONS FOR ONE SYMBOL (ASTRA ONLY)
# ============================================================

def close_symbol_positions(symbol: str, *, deviation: int = 10) -> List[Dict[str, Any]]:
    """
    Close all *Astra* positions for this symbol for *today*.
    (Comment prefix Astra-DDMMYY-)

    Uses your normalize_trade_result + format_discord_trade_message.
    """
    _ensure_mt5()

    results: List[Dict[str, Any]] = []

    positions = get_astra_positions_today(symbol) or ()
    if not positions:
        # No Astra positions for today on this symbol – just log & exit
        n = normalize_trade_result(
            context={
                "symbol": symbol,
                "side": "close",
                "comment": "No Astra positions to close for today",
            }
        )
        ch, msg = format_discord_trade_message(n)
        print(msg)
        send_discord_message(ch, msg)
        return results

    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            n = normalize_trade_result(
                context={
                    "symbol": symbol,
                    "side": "close",
                    "comment": "No tick data during close_symbol_positions",
                }
            )
            ch, msg = format_discord_trade_message(n)
            print(msg)
            send_discord_message(ch, msg)
            continue

        is_long = (pos.type == mt5.POSITION_TYPE_BUY)
        close_type = mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY
        price = tick.bid if is_long else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "type": close_type,
            "position": int(getattr(pos, "ticket", 0)),
            "price": float(price),
            "volume": float(pos.volume),
            "deviation": int(deviation),
            "type_filling": mt5.ORDER_FILLING_FOK,
            "type_time": mt5.ORDER_TIME_GTC,
            "comment": make_order_comment("Astra-close"),
        }
        res = mt5.order_send(request)
        n = normalize_trade_result(
            request=request,
            response=res,
            context={
                "symbol": symbol,
                "side": "SELL" if is_long else "BUY",
                "volume": float(pos.volume),
                "price": price,
            },
        )
        ch, msg = format_discord_trade_message(n)
        print(msg)
        send_discord_message(ch, msg)
        results.append(n)

    return results


# ============================================================
#   QUICK LOCAL TEST (⚠ will trade if MT5 is LIVE!)
# ============================================================

if __name__ == "__main__":
    # ⚠ Only run this when you *want* a real test.
    posiiton=place_trade("XAGUSD", "buy", 0.5)
    print(posiiton)
    # close_symbol_positions("XAGUSD")
    print("trade.py loaded. Manual tests commented out.")
