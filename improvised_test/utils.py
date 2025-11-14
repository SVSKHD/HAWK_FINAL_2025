from typing import Any, Dict, Optional, Tuple
import MetaTrader5 as mt5
# --- Normalization helpers ----------------------------------------------------
TradeType = str  # or Literal["buy","sell"] if you like

def _to_mapping(obj: Any) -> Dict[str, Any]:
    """Best-effort convert MT5 result objects or dict-like into a plain dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    # MT5 result is a struct-like object: collect public attributes
    out: Dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            val = getattr(obj, name)
        except Exception:
            continue
        # Filter out callables
        if callable(val):
            continue
        out[name] = val
    return out

def _get_any(src: Dict[str, Any], *names: str, default: Any = None) -> Any:
    """Return the first present key (case-insensitive) from 'names' in 'src'."""
    if not src:
        return default
    lower = {k.lower(): k for k in src.keys()}
    for nm in names:
        k = lower.get(nm.lower())
        if k is not None:
            return src[k]
    return default

def _retcode_name(code: Optional[int]) -> str:
    if code is None:
        return "UNKNOWN(None)"
    try:
        import MetaTrader5 as mt5  # local import to avoid circular issues
        for name in dir(mt5):
            if name.startswith("TRADE_RETCODE_") and getattr(mt5, name, None) == code:
                return name
    except Exception:
        pass
    # Common success code in MT5 is 10009
    return "TRADE_RETCODE_DONE" if code == 10009 else f"UNKNOWN({code})"

def normalize_trade_result(
    *,
    request: Optional[Dict[str, Any]] = None,
    response: Any = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Produce a uniform dict no matter what shape the MT5 response has.
    Keys included only when available.
    """
    req = dict(request or {})
    raw = _to_mapping(response or {})
    ctx = dict(context or {})

    # Extract common fields with multiple possible names
    symbol   = ctx.get("symbol")   or _get_any(req, "symbol")
    side     = ctx.get("side")     or ctx.get("type") or _get_any(req, "side", "trade_type", "type")
    volume   = ctx.get("volume")   or _get_any(req, "volume", "vol")
    price    = ctx.get("price")    or _get_any(req, "price", "open_price", "Ask", "Bid")
    comment  = ctx.get("comment")  or _get_any(req, "comment") or _get_any(raw, "comment")
    # Result identifiers
    retcode  = _get_any(raw, "retcode", "ret_code", "ret")
    deal_id  = _get_any(raw, "deal", "deal_id")
    order_id = _get_any(raw, "order", "order_id")
    pos_id   = _get_any(raw, "position", "position_id", "ticket", "pos_id")

    # Determine success robustly
    DONE = {ret for ret in (getattr(mt5, "TRADE_RETCODE_DONE", None), 10009) if ret is not None}
    ok = retcode in DONE

    norm: Dict[str, Any] = {
        "ok": ok,
        "symbol": symbol,
        "side": side,
        "volume": volume,
        "price": price,
        "retcode": retcode,
        "retcode_name": _retcode_name(retcode),
        "order_id": order_id,
        "deal_id": deal_id,
        "position_id": pos_id,
    }
    if comment is not None:
        norm["comment"] = comment
    if req:
        norm["request"] = req
    if raw:
        norm["raw_response"] = raw
    if ctx:
        norm["context"] = ctx
    # Remove None values to keep it clean
    return {k: v for k, v in norm.items() if v is not None}

def format_discord_trade_message(n: Dict[str, Any]) -> Tuple[str, str]:
    """
    Build a Discord-friendly message from a normalized result.
    Returns (channel, message). Channel is 'info' on success, 'critical' on failure.
    """
    ok = bool(n.get("ok"))
    ch = "info" if ok else "critical"
    emoji = "✅" if ok else "❌"
    title = "**Trade Executed Successfully**" if ok else "**Trade Failed**"

    # Safely pull fields
    def S(key, fallback="N/A"):
        v = n.get(key)
        return str(v) if v is not None else fallback

    parts = [
        f"{emoji} {title}",
        f"**Symbol:** {S('symbol')}",
        f"**Type:** {S('side').upper() if isinstance(n.get('side'), str) else S('side')}",
        f"**Volume:** {S('volume')}",
    ]
    if "price" in n:
        parts.append(f"**Price:** {S('price')}")
    parts.append(f"**Retcode:** {S('retcode')} ({S('retcode_name')})")
    if "comment" in n:
        parts.append(f"**Comment:** {S('comment')}")
    if "order_id" in n or "deal_id" in n:
        parts.append(f"**Order ID:** {S('order_id')}  **Deal ID:** {S('deal_id')}")
    if not ok and "position_id" in n:
        parts.append(f"**Position ID:** {S('position_id')}")
    return ch, "\n".join(parts)



def _retcode_name(code: int) -> str:
    # best-effort mapping to human-friendly name
    for name in dir(mt5):
        if name.startswith("TRADE_RETCODE_") and getattr(mt5, name, None) == code:
            return name
    return f"UNKNOWN({code})"

def _format_success(symbol: str, trade_type: TradeType, volume: float, price: float, result) -> str:
    return (
        "✅ **Trade Executed Successfully**\n"
        f"**Symbol:** {symbol}\n"
        f"**Type:** {trade_type.upper()}\n"
        f"**Volume:** {volume}\n"
        f"**Price:** {price}\n"
        f"**Order ID:** {getattr(result, 'order', 'N/A')}\n"
        f"**Deal ID:** {getattr(result, 'deal', 'N/A')}\n"
        f"**Retcode:** {getattr(result, 'retcode', 'N/A')} ({_retcode_name(getattr(result, 'retcode', -1))})\n"
        f"**Comment:** {getattr(result, 'comment', '')}"
    )

def _format_failure(symbol: str, trade_type: TradeType, volume: float, price: float, result) -> str:
    return (
        "❌ **Trade Failed**\n"
        f"**Symbol:** {symbol}\n"
        f"**Type:** {trade_type.upper()}\n"
        f"**Volume:** {volume}\n"
        f"**Price (attempted):** {price}\n"
        f"**Retcode:** {getattr(result, 'retcode', 'N/A')} ({_retcode_name(getattr(result, 'retcode', -1))})\n"
        f"**Comment:** {getattr(result, 'comment', '')}\n"
        f"**Order ID:** {getattr(result, 'order', 'N/A')}  **Deal ID:** {getattr(result, 'deal', 'N/A')}"
    )
