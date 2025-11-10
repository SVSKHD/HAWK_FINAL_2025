import MetaTrader5 as mt5
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from notify import send_discord_message
from mt5 import init_mt5
from utils import format_discord_trade_message, _format_failure, _format_success, normalize_trade_result

# optional: tighten type a bit

TradeType = str  # or Literal["buy","sell"] if you like


import re
from datetime import datetime, timezone

MAX_COMMENT_LEN = 31  # common MT5/broker limit (some allow 32)

_ascii_re = re.compile(r"[^\x20-\x7E]")  # printable ASCII only

def make_order_comment(base: Optional[str] = None) -> str:
    """
    Build a broker-safe order comment:
    - ASCII only (no emojis, no en-dash)
    - <= 31 chars
    """
    # keep it short: YYYYMMDD-HHMMSS (15 chars)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    # avoid fancy punctuation: use plain hyphen
    base = (base or "AstraBot").strip().replace("–", "-").replace("—", "-")
    raw = f"{base} {ts}"

    # remove non-ASCII
    raw = _ascii_re.sub("", raw)

    # trim to limit
    return raw[:MAX_COMMENT_LEN]



def place_trade(symbol: str, trade_type: TradeType, volume: float, comment: Optional[str] = None):
    init_mt5("place_trade from trade.py")

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        msg = f"❌ No tick data for {symbol}"
        print(msg)
        send_discord_message("critical", msg)
        return False

    is_buy = (trade_type == "buy")
    price = tick.ask if is_buy else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    order_comment = make_order_comment(comment or "AstraBot")

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": float(price),
        "deviation": 10,
        "comment": order_comment,
        "type_filling": mt5.ORDER_FILLING_FOK,
        "type_time": mt5.ORDER_TIME_GTC
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




from typing import List, Dict, Any, Optional
import MetaTrader5 as mt5

def close_all_trades(*, deviation: int = 10,
                     include_masks: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Close ALL open positions, optionally filtering by Python-side include masks on symbol.
    - include_masks: list of substrings; a position is kept if any mask is in pos.symbol.
                     e.g. ["BTC", "XBT", "USD"] to catch BTC, XBT, BTCUSD, BTCUSDT, etc.
    """
    results: List[Dict[str, Any]] = []

    # Ensure terminal is ready
    if not mt5.initialize():
        code, details = mt5.last_error()
        raise RuntimeError(f"MT5 initialize failed: {code} {details}")

    # Don’t use 'group' filter; fetch all and filter in Python
    positions = mt5.positions_get()
    if positions is None:
        code, details = mt5.last_error()
        print(f"[positions_get=None] last_error={code} {details}")
        return results

    if len(positions) == 0:
        print("[close_all_trades] No open positions.")
        return results

    # Optional filter by symbol substring(s)
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
        # Minimal normalized record (replace with your normalize_trade_result if you have it)
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
        print(f"[closed] {symbol} {rec['closed_side']} vol={rec['volume']} price={rec['price']} ret={rec['response']['retcode']}")
        results.append(rec)

    return results



def close_symbol_positions(symbol: str, *, deviation: int = 10) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    positions = mt5.positions_get(symbol=symbol) or ()
    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            n = normalize_trade_result(
                context={"symbol": symbol, "side": "close", "comment": "No tick data during close_symbol_positions"}
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
            "comment": f"bot-2025 close {'long' if is_long else 'short'}",
        }
        res = mt5.order_send(request)
        n = normalize_trade_result(
            request=request,
            response=res,
            context={"symbol": symbol, "side": "SELL" if is_long else "BUY", "volume": float(pos.volume), "price": price},
        )
        ch, msg = format_discord_trade_message(n)
        print(msg)
        send_discord_message(ch, msg)
        results.append(n)
    return results




# quick test (will actually try to trade if MT5 connected!)
if __name__ == "__main__":
    place_trade("BTCUSD", "buy", 0.5)
    # positions=close_symbol_positions("EURUSD")
    # closed_positions = close_all_trades()
    # print("closed", closed_positions)
    print(50*"*==")
    # close_positions_by_symbol = close_symbol_positions("BTCUSD")
    print(50 * "*==")

