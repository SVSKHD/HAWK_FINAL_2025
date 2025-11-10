from __future__ import annotations
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, Tuple, Literal
import MetaTrader5 as mt5
from mt5 import init_mt5
from zoneinfo import ZoneInfo
from datetime import datetime
from typing import Optional, Tuple, Dict, Any


WeekendPolicy = Literal["skip", "previous_trading_day", "next_trading_day"]

def _select_symbol(symbol: str) -> None:
    info = mt5.symbol_info(symbol)
    if info is None:
        code, details = mt5.last_error()
        raise RuntimeError(f"Symbol '{symbol}' not found. last_error={code} {details}")
    if not info.visible and not mt5.symbol_select(symbol, True):
        code, details = mt5.last_error()
        raise RuntimeError(f"Failed to select symbol '{symbol}'. last_error={code} {details}")

def _mt5_tf(label: str):
    return {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
    }[label]

def _is_weekend(d: date) -> bool:
    # Saturday=5, Sunday=6
    return d.weekday() >= 5

def _shift_trading_day(
    d: date, policy: WeekendPolicy
) -> tuple[date, Optional[str]]:
    """
    Shift weekend dates per policy. Returns (date, reason_note)
    """
    if not _is_weekend(d):
        return d, None
    if policy == "skip":
        return d, "weekend_no_shift"
    if policy == "previous_trading_day":
        # Sat -> Fri (-1), Sun -> Fri (-2)
        delta = 1 if d.weekday() == 5 else 2
        return d - timedelta(days=delta), f"shifted_from_weekend_to_previous_trading_day({delta}d)"
    if policy == "next_trading_day":
        # Sat -> Mon (+2), Sun -> Mon (+1)
        delta = 2 if d.weekday() == 5 else 1
        return d + timedelta(days=delta), f"shifted_from_weekend_to_next_trading_day({delta}d)"
    return d, None

def compute_target_server_dt(
    requested_date: Optional[date],
    server_timezone: str,
    hour: int = 8,
    minute: int = 0,
    weekend_policy: WeekendPolicy = "previous_trading_day",
) -> tuple[datetime, Optional[str]]:
    tz = ZoneInfo(server_timezone)
    d = (datetime.now(tz).date() if requested_date is None else requested_date)
    d2, note = _shift_trading_day(d, weekend_policy)
    return datetime(d2.year, d2.month, d2.day, hour, minute, tzinfo=tz), note

def _diagnostics(symbol: str, tf_label: str, target_dt_srv: datetime, search_minutes: int):
    ti = mt5.terminal_info()
    vi = mt5.version()
    si = mt5.symbol_info(symbol)
    code, details = mt5.last_error()
    print(
        "[MT5] No bars found",
        {
            "symbol": symbol,
            "tf": tf_label,
            "target_server": target_dt_srv.isoformat(),
            "window_min": search_minutes,
            "terminal_connected": bool(ti),
            "version": vi,
            "symbol_visible": (si.visible if si else None),
            "last_error": [code, details],
        },
        flush=True,
    )

def _first_bar_at_or_after(
    symbol: str,
    tf_label: str,
    target_dt_srv: datetime,
    search_minutes: int = 90,
) -> Optional[Dict[str, Any]]:
    tf = _mt5_tf(tf_label)

    # main attempt
    start = target_dt_srv
    end = target_dt_srv + timedelta(minutes=search_minutes)
    rates = mt5.copy_rates_range(symbol, tf, start, end)

    # widen forward window (Mon openings etc.)
    if rates is None or len(rates) == 0:
        start_fb = target_dt_srv - timedelta(minutes=5)
        end_fb = target_dt_srv + timedelta(minutes=max(180, search_minutes))
        rates = mt5.copy_rates_range(symbol, tf, start_fb, end_fb)

    # try next available bar strictly AFTER the target
    if rates is None or len(rates) == 0:
        rates = mt5.copy_rates_from(symbol, tf, target_dt_srv, 1)

    if rates is None or len(rates) == 0:
        _diagnostics(symbol, tf_label, target_dt_srv, search_minutes)
        return None

    def _row_time_to_dt(row_time) -> datetime:
        if isinstance(row_time, (int, float)):
            return datetime.fromtimestamp(row_time, target_dt_srv.tzinfo)
        if isinstance(row_time, datetime):
            return row_time.astimezone(target_dt_srv.tzinfo)
        return target_dt_srv - timedelta(days=3650)

    # pick first >= target, else take the first available
    for r in rates:
        bar_dt = _row_time_to_dt(r["time"])
        if bar_dt >= target_dt_srv:
            return {"bar_time_server": bar_dt.isoformat(), "open_price": float(r["open"])}

    # fallback to the very first item
    r0 = rates[0]
    bar_dt0 = _row_time_to_dt(r0["time"])
    return {"bar_time_server": bar_dt0.isoformat(), "open_price": float(r0["open"])}


def _nearest_previous_bar(
    symbol: str, tf_label: str, target_dt_srv: datetime, lookback_minutes: int = 240
) -> Optional[Dict[str, Any]]:
    """Last resort: find the most recent bar BEFORE the target."""
    tf = _mt5_tf(tf_label)
    start = target_dt_srv - timedelta(minutes=lookback_minutes)
    end = target_dt_srv
    rates = mt5.copy_rates_range(symbol, tf, start, end)
    if rates is None or len(rates) == 0:
        return None
    r = rates[-1]  # last one before target
    if isinstance(r["time"], (int, float)):
        bar_dt = datetime.fromtimestamp(r["time"], target_dt_srv.tzinfo)
    else:
        bar_dt = r["time"].astimezone(target_dt_srv.tzinfo)
    return {"bar_time_server": bar_dt.isoformat(), "open_price": float(r["open"])}


def _nearest_previous_bar(
    symbol: str, tf_label: str, target_dt_srv: datetime, lookback_minutes: int = 240
) -> Optional[Dict[str, Any]]:
    """Last resort: find the most recent bar BEFORE the target."""
    tf = _mt5_tf(tf_label)
    start = target_dt_srv - timedelta(minutes=lookback_minutes)
    end = target_dt_srv
    rates = mt5.copy_rates_range(symbol, tf, start, end)
    if not rates:
        return None
    r = rates[-1]  # last one before target
    if isinstance(r["time"], (int, float)):
        bar_dt = datetime.fromtimestamp(r["time"], target_dt_srv.tzinfo)
    else:
        bar_dt = r["time"].astimezone(target_dt_srv.tzinfo)
    return {"bar_time_server": bar_dt.isoformat(), "open_price": float(r["open"])}

def get_8am_snapshot(
    symbol: str,
    requested_date: Optional[date],
    server_timezone: str,
    ist_timezone: str = "Asia/Kolkata",
    timeframes: Tuple[str, ...] = ("M1", "M5", "M15", "M30", "H1"),
    weekend_policy: WeekendPolicy = "previous_trading_day",
) -> Dict[str, Any]:
    """
    Build the 8AM snapshot across timeframes with weekend- and gap-handling.
    - If requested_date is None → uses 'today' in server tz, then weekend policy shift.
    - Selects the symbol and ensures MT5 is initialized.
    - Tries at/after 08:00 server time, with multiple fallbacks.
    """
    init_mt5("from start_prices.py")
    _select_symbol(symbol)

    target_srv, weekend_note = compute_target_server_dt(
        requested_date, server_timezone, 8, 0, weekend_policy=weekend_policy
    )
    tz_ist = ZoneInfo(ist_timezone)
    eight_am_ist = target_srv.astimezone(tz_ist)

    snapshot: Dict[str, Any] = {
        "symbol": symbol,
        "requested_date": target_srv.date().isoformat(),
        "server_timezone": server_timezone,
        "anchors": {
            "eight_am_server": target_srv.isoformat(),
            "eight_am_ist": eight_am_ist.isoformat(),
        },
        "timeframes": {},
        "meta": {"source": "MT5", "version": "1.1.0"},
    }
    if weekend_note:
        snapshot["meta"]["weekend_policy"] = weekend_note

    price_at_anchor = None
    for tf in timeframes:
        bar = _first_bar_at_or_after(symbol, tf, target_srv, search_minutes=90)
        if bar is None:
            # last fallback: nearest previous bar (up to 4h lookback)
            bar = _nearest_previous_bar(symbol, tf, target_srv, lookback_minutes=240)
            if bar:
                bar["fallback"] = "nearest_previous_bar"
        snapshot["timeframes"][tf] = bar
        if tf == "M1" and bar:
            price_at_anchor = bar["open_price"]

    snapshot["anchors"]["price_at_anchor"] = price_at_anchor
    return snapshot



def get_current_price(symbol):
    if not symbol:
        return "Please provide a valid symbol."
    try:
        init_mt5("from_current_prices.py")
        _select_symbol(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return f"Could not retrieve tick data for symbol '{symbol}'."
        return {
            "symbol": symbol,
            "bid": tick.bid,
            "ask": tick.ask,
            "last": tick.last,
            "time": datetime.fromtimestamp(tick.time).isoformat(),
        }
    except Exception as e:
        return f"An error occurred: {str(e)}"


from typing import NamedTuple

class HighLow(NamedTuple):
    high: float | None
    low: float | None
    start_iso: str
    end_iso: str
    bars: int

def _copy_m1_range(symbol: str, start_dt: datetime, end_dt: datetime):
    tf = mt5.TIMEFRAME_M1
    rates = mt5.copy_rates_range(symbol, tf, start_dt, end_dt)
    return rates if (rates is not None and len(rates) > 0) else None

def get_recent_high_low(
    symbol: str,
    server_timezone: str,
    lookback_minutes: int = 60,
) -> HighLow:
    """
    High/low over the last N minutes (M1 bars). Returns None values if no bars.
    """
    init_mt5("from recent_high_low_prices-m1.py")
    _select_symbol(symbol)
    tz = ZoneInfo(server_timezone)
    end_dt = datetime.now(tz)
    start_dt = end_dt - timedelta(minutes=lookback_minutes)
    rates = _copy_m1_range(symbol, start_dt, end_dt)
    if rates is None:
        return HighLow(None, None, start_dt.isoformat(), end_dt.isoformat(), 0)

    highs = [float(r["high"]) for r in rates]
    lows = [float(r["low"]) for r in rates]
    return HighLow(max(highs), min(lows), start_dt.isoformat(), end_dt.isoformat(), len(rates))

def get_high_low_since_anchor(
    symbol: str,
    anchor_dt_srv: datetime,
) -> HighLow:
    """
    High/low from a server-time anchor dt (e.g., 08:00) up to 'now' (M1 bars).
    """
    init_mt5("from high_low_prices.py")
    _select_symbol(symbol)
    end_dt = datetime.now(anchor_dt_srv.tzinfo)
    if end_dt <= anchor_dt_srv:
        # nothing to compute
        return HighLow(None, None, anchor_dt_srv.isoformat(), end_dt.isoformat(), 0)

    rates = _copy_m1_range(symbol, anchor_dt_srv, end_dt)
    if rates is None:
        return HighLow(None, None, anchor_dt_srv.isoformat(), end_dt.isoformat(), 0)

    highs = [float(r["high"]) for r in rates]
    lows = [float(r["low"]) for r in rates]
    return HighLow(max(highs), min(lows), anchor_dt_srv.isoformat(), end_dt.isoformat(), len(rates))

def get_extremes_relative_to_price(
    symbol: str,
    reference_price: float,
    since_dt_srv: datetime | None,
) -> Dict[str, Any]:
    """
    Since 'since_dt_srv', compute:
    - highest high strictly ABOVE reference_price (or None if never breached)
    - lowest low strictly BELOW reference_price (or None if never breached)
    Returns breach flags and the first breach timestamps if found.
    """
    init_mt5("from relative_high_low_prices.py")
    _select_symbol(symbol)

    # Handle None start — default to last 24h window
    if since_dt_srv is None:
        tz = datetime.now().astimezone().tzinfo
        since_dt_srv = datetime.now(tz) - timedelta(hours=24)

    end_dt = datetime.now(since_dt_srv.tzinfo)
    rates = _copy_m1_range(symbol, since_dt_srv, end_dt)

    if rates is None:
        return {
            "reference_price": reference_price,
            "since_server": since_dt_srv.isoformat(),
            "highest_above": None,
            "lowest_below": None,
            "breached_up": False,
            "breached_down": False,
            "first_up_break_time": None,
            "first_down_break_time": None,
            "bars": 0,
        }

    highest_above = None
    lowest_below = None
    first_up_break_time = None
    first_down_break_time = None

    for r in rates:
        hi = float(r["high"])
        lo = float(r["low"])
        rt = r["time"]

        # Robust conversion: works for numpy.int64, float, datetime
        if isinstance(rt, datetime):
            bar_dt = rt.astimezone(since_dt_srv.tzinfo)
        else:
            try:
                bar_dt = datetime.fromtimestamp(int(rt), since_dt_srv.tzinfo)
            except Exception:
                # fallback if weird type
                bar_dt = since_dt_srv

        if hi > reference_price:
            highest_above = hi if highest_above is None or hi > highest_above else highest_above
            if first_up_break_time is None:
                first_up_break_time = bar_dt.isoformat()

        if lo < reference_price:
            lowest_below = lo if lowest_below is None or lo < lowest_below else lowest_below
            if first_down_break_time is None:
                first_down_break_time = bar_dt.isoformat()

    return {
        "reference_price": reference_price,
        "since_server": since_dt_srv.isoformat(),
        "highest_above": highest_above,
        "lowest_below": lowest_below,
        "breached_up": highest_above is not None,
        "breached_down": lowest_below is not None,
        "first_up_break_time": first_up_break_time,
        "first_down_break_time": first_down_break_time,
        "bars": len(rates),
    }



def _build_snapshot_for_anchor(
    symbol: str,
    target_srv: datetime,
    ist_timezone: str,
    timeframes: Tuple[str, ...],
    weekend_note: Optional[str],
) -> Dict[str, Any]:
    tz_ist = ZoneInfo(ist_timezone)
    anchor_ist = target_srv.astimezone(tz_ist)

    snapshot: Dict[str, Any] = {
        "symbol": symbol,
        "requested_date": target_srv.date().isoformat(),
        "server_timezone": str(target_srv.tzinfo),
        "anchors": {
            "anchor_server": target_srv.isoformat(),
            "anchor_ist": anchor_ist.isoformat(),
        },
        "timeframes": {},
        "meta": {"source": "MT5", "version": "1.1.0"},
    }
    if weekend_note:
        snapshot["meta"]["weekend_policy"] = weekend_note

    price_at_anchor = None
    for tf in timeframes:
        bar = _first_bar_at_or_after(symbol, tf, target_srv, search_minutes=90)
        if bar is None:
            bar = _nearest_previous_bar(symbol, tf, target_srv, lookback_minutes=240)
            if bar:
                bar["fallback"] = "nearest_previous_bar"
        snapshot["timeframes"][tf] = bar
        if tf == "M1" and bar:
            price_at_anchor = bar["open_price"]

    snapshot["anchors"]["price_at_anchor"] = price_at_anchor
    return snapshot

def get_snapshot_at_server_time(
    symbol: str,
    requested_date: Optional[date],
    server_timezone: str,
    hour_server: int,
    minute_server: int,
    ist_timezone: str = "Asia/Kolkata",
    timeframes: Tuple[str, ...] = ("M1","M5","M15","M30","H1"),
    weekend_policy: WeekendPolicy = "previous_trading_day",
) -> Dict[str, Any]:
    """
    Build snapshot at a specific *server time* (e.g., 11:30 server).
    """
    init_mt5("from server_prices.py")
    _select_symbol(symbol)

    target_srv, weekend_note = compute_target_server_dt(
        requested_date, server_timezone,
        hour=hour_server, minute=minute_server,
        weekend_policy=weekend_policy
    )
    snap = _build_snapshot_for_anchor(symbol, target_srv, ist_timezone, timeframes, weekend_note)
    snap["anchors"]["label"] = f"{hour_server:02d}:{minute_server:02d} server"
    return snap

def get_snapshot_at_ist_time(
    symbol: str,
    requested_date: Optional[date],
    server_timezone: str,
    ist_hour: int,
    ist_minute: int,
    ist_timezone: str = "Asia/Kolkata",
    timeframes: Tuple[str, ...] = ("M1","M5","M15","M30","H1"),
    weekend_policy: WeekendPolicy = "previous_trading_day",
) -> Dict[str, Any]:
    """
    Build snapshot anchored at a specific *IST time* (e.g., 11:30 IST).
    Internally converts to server time and fetches the first bar at/after that moment.
    """
    init_mt5("from server_ist_prices.py")
    _select_symbol(symbol)

    server_tz = ZoneInfo(server_timezone)
    tz_ist = ZoneInfo(ist_timezone)
    d = datetime.now(tz_ist).date() if requested_date is None else requested_date
    ist_dt = datetime(d.year, d.month, d.day, ist_hour, ist_minute, tzinfo=tz_ist)
    server_dt = ist_dt.astimezone(server_tz)

    # Apply weekend policy on server calendar day
    shifted_date, weekend_note = _shift_trading_day(server_dt.date(), weekend_policy)
    target_srv = datetime(
        shifted_date.year, shifted_date.month, shifted_date.day,
        server_dt.hour, server_dt.minute, tzinfo=server_tz
    )

    snap = _build_snapshot_for_anchor(symbol, target_srv, ist_timezone, timeframes, weekend_note)
    snap["anchors"]["label"] = f"{ist_hour:02d}:{ist_minute:02d} IST (converted)"
    return snap



if __name__ == "__main__":
    # Use your broker's server TZ (often Etc/GMT-3). Try without date (auto today + weekend shift).
    from datetime import date as _date
    symbol = "XAUUSD"
    SERVER_TZ = "Etc/GMT-3"
    start_price = get_8am_snapshot(symbol, requested_date=None, server_timezone=SERVER_TZ)
    start_price_at_anchor = start_price['anchors']['price_at_anchor']
    print("start:",start_price['anchors']['price_at_anchor'])
    print("current",get_current_price(symbol))
    print("high and low",get_extremes_relative_to_price(symbol,start_price_at_anchor,None))
