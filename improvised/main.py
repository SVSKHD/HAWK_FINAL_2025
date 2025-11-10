# main_runner.py
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time
from typing import Dict, Any, Optional

from config import SERVER_TZ, IST, ENABLED_SYMBOLS, HOUR, MINUTES
from prices import get_snapshot_at_ist_time, get_current_price, get_high_low_since_anchor
from executor import execute_place_and_close
from notify import send_discord_message

# Toggle this to False for real trading
DRY_RUN = True

state: Dict[str, Dict[str, Any]] = {
    s: {
        "anchor_server_iso": None,
        "anchor_dt_srv": None,
        "start_price": None,
        "previous_executables": None,
        "last_snapshot_date_ist": None,
        "start_notified_date_ist": None,       # de-dup "Start Price Set"
        "last_price_notify_date_ist": None,    # de-dup daily scheduled price update
        "boot_price_notified": False,          # ensure boot notify only once per run
    } for s in ENABLED_SYMBOLS
}

def _today_ist_str() -> str:
    return datetime.now(IST).date().isoformat()

def _send_start_price_notify(symbol: str) -> None:
    """Send 'Start Price Set' once per IST day after we take the snapshot."""
    today = _today_ist_str()
    # must have a snapshot today
    if state[symbol]["last_snapshot_date_ist"] != today:
        return
    # avoid duplicates
    if state[symbol]["start_notified_date_ist"] == today:
        return

    start_price = state[symbol]["start_price"]
    anchor_server_iso = state[symbol]["anchor_server_iso"]
    anchor_dt_srv = state[symbol]["anchor_dt_srv"]

    # current + extremes (best-effort)
    current = None
    current_time = None
    try:
        tick = get_current_price(symbol)
        if isinstance(tick, dict):
            current = float(tick.get("last") or tick.get("bid") or tick.get("ask"))
            current_time = tick.get("time")
    except Exception as e:
        print(f"[WARN] get_current_price fail for {symbol}: {e}")

    high = low = current
    try:
        if anchor_dt_srv:
            hl = get_high_low_since_anchor(symbol, anchor_dt_srv)
            if hl:
                high = hl.high if hl.high is not None else current
                low  = hl.low  if hl.low  is not None else current
    except Exception as e:
        print(f"[WARN] get_high_low_since_anchor fail for {symbol}: {e}")

    msg = (
        f"🟢 **Start Price Set** — {symbol}\n"
        f"• Anchor (server): `{anchor_server_iso}`\n"
        f"• Start: `{start_price}`\n"
        f"• Current @ snapshot check: `{current}` (tick: `{current_time}`)\n"
        f"• High since anchor: `{high}`\n"
        f"• Low since anchor: `{low}`"
    )
    send_discord_message("info", msg)
    state[symbol]["start_notified_date_ist"] = today

def _send_price_update(symbol: str, *, force: bool = False) -> None:
    """
    Send a compact price update:
      • On boot (force=True)
      • At scheduled HOUR:MINUTES IST once per day
    """
    now_ist = datetime.now(IST)
    today = _today_ist_str()

    if force:
        # boot-time notify (once per process run)
        if state[symbol]["boot_price_notified"]:
            return
    else:
        # scheduled notify: once per IST day, within +/- 60s of target
        if state[symbol]["last_price_notify_date_ist"] == today:
            return
        target = now_ist.replace(hour=HOUR, minute=MINUTES, second=0, microsecond=0)
        if abs((now_ist - target).total_seconds()) > 60:
            return  # not in the 1-minute window

    # Gather data
    start = state[symbol].get("start_price")
    anchor_dt_srv = state[symbol].get("anchor_dt_srv")

    tick = get_current_price(symbol)
    if not isinstance(tick, dict):
        print(f"[WARN] price update skipped (no tick) for {symbol}: {tick}")
        return

    current = float(tick.get("last") or tick.get("bid") or tick.get("ask"))
    high = low = current
    if anchor_dt_srv:
        try:
            hl = get_high_low_since_anchor(symbol, anchor_dt_srv)
            if hl:
                high = hl.high if hl.high is not None else current
                low  = hl.low  if hl.low  is not None else current
        except Exception as e:
            print(f"[WARN] extremes fetch failed for {symbol}: {e}")

    msg = (
        f"📊 **{symbol} Price Update**\n"
        f"• Start (anchor): `{start}`\n"
        f"• Current: `{current}`\n"
        f"• High since anchor: `{high}`\n"
        f"• Low since anchor: `{low}`\n"
        f"• Time (IST): `{now_ist.strftime('%H:%M:%S')}`"
    )
    send_discord_message("info", msg)

    if force:
        state[symbol]["boot_price_notified"] = True
    else:
        state[symbol]["last_price_notify_date_ist"] = today

def _ensure_daily_anchor(symbol: str):
    """Take (or ensure we have) the daily anchor at HOUR:MINUTES IST for a symbol."""
    today = _today_ist_str()
    if state[symbol]["last_snapshot_date_ist"] == today:
        return

    now_ist = datetime.now(IST)
    target = now_ist.replace(hour=HOUR, minute=MINUTES, second=0, microsecond=0)
    if now_ist < target:
        return

    # Take snapshot (start price at HOUR:MINUTES IST converted to server tz)
    snap = get_snapshot_at_ist_time(
        symbol=symbol,
        requested_date=None,
        server_timezone=SERVER_TZ,
        ist_hour=HOUR,
        ist_minute=MINUTES,
    )
    start_price = snap["anchors"]["price_at_anchor"]
    anchor_server_iso = snap["anchors"]["anchor_server"]
    anchor_dt_srv = datetime.fromisoformat(anchor_server_iso)

    state[symbol]["start_price"] = start_price
    state[symbol]["anchor_server_iso"] = anchor_server_iso
    state[symbol]["anchor_dt_srv"] = anchor_dt_srv
    state[symbol]["last_snapshot_date_ist"] = today

    print(f"\n=== {symbol} — snapshot @ {HOUR:02d}:{MINUTES:02d} IST ===")
    print("start_price:", start_price)
    print("anchor_server:", anchor_server_iso)

    # send the Start Price notice once/day
    _send_start_price_notify(symbol)

def _one_tick(symbol: str):
    if state[symbol]["anchor_dt_srv"] is None or state[symbol]["start_price"] is None:
        print(f"\n--- {symbol} --- waiting for {HOUR:02d}:{MINUTES:02d} IST snapshot …")
        return

    start = state[symbol]["start_price"]
    tick = get_current_price(symbol)
    if not isinstance(tick, dict):
        print(f"\n--- {symbol} tick error: {tick}")
        return

    current = float(tick.get("last") or tick.get("bid") or tick.get("ask"))

    hl = get_high_low_since_anchor(symbol, state[symbol]["anchor_dt_srv"])
    high: Optional[float] = hl.high if hl and hl.high is not None else current
    low: Optional[float]  = hl.low  if hl and hl.low  is not None else current

    prev_exec = state[symbol]["previous_executables"]

    resp = execute_place_and_close(
        symbol=symbol,
        start=start,
        current=current,
        high=high,
        low=low,
        previous_executables=prev_exec,
        dry_run=DRY_RUN,
    )

    state[symbol]["previous_executables"] = resp.get("executables")
    print(f"[{symbol}] action={resp['action']} dir={resp['direction']} scale={resp['threshold_scale']} note={resp.get('note')}")

def main():
    print(f"Runner: daily snapshot at {HOUR:02d}:{MINUTES:02d} IST; per-second loop. DRY_RUN={DRY_RUN}")

    # --- Boot-time price update for all symbols ---
    for s in ENABLED_SYMBOLS:
        _send_price_update(s, force=True)

    while True:
        try:
            # ensure anchors (and send 'Start Price Set' after snapshot)
            for s in ENABLED_SYMBOLS:
                _ensure_daily_anchor(s)

            # scheduled price update at HOUR:MINUTES IST (once/day)
            for s in ENABLED_SYMBOLS:
                _send_price_update(s, force=False)

            # per-second trading loop
            for s in ENABLED_SYMBOLS:
                _one_tick(s)

            time.sleep(1)
        except KeyboardInterrupt:
            print("\nexit"); break
        except Exception as e:
            print("[WARN]", e)
            time.sleep(1)

if __name__ == "__main__":
    main()
