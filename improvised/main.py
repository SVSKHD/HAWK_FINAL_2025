# main_runner.py
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time
from typing import Dict, Any, Optional

from config import SERVER_TZ, IST, ENABLED_SYMBOLS, HOUR, MINUTES, NOTIFY_DELAY_SEC, SNAPSHOT_GRACE_SEC, DRY_RUN
from prices import get_snapshot_at_ist_time, get_current_price, get_high_low_since_anchor
from executor import execute_place_and_close
from notify import send_discord_message
from mt5 import init_mt5
import metatrader5 as mt5



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
    now_ist = datetime.now(IST)
    today = _today_ist_str()

    has_today_snapshot = (state[symbol].get("last_snapshot_date_ist") == today)
    start = state[symbol].get("start_price")

    if force:
        # Boot-time update ONLY if start_price exists (prevents Start=None)
        if state[symbol]["boot_price_notified"] or not (has_today_snapshot and start is not None):
            return
    else:
        # Scheduled update: require snapshot + start price
        if not (has_today_snapshot and start is not None):
            return
        if state[symbol]["last_price_notify_date_ist"] == today:
            return

        target = now_ist.replace(hour=HOUR, minute=MINUTES, second=0, microsecond=0)
        send_time = target + timedelta(seconds=NOTIFY_DELAY_SEC)

        # Must be after 09:05 (with a small 60s window)
        if not (now_ist >= send_time and (now_ist - send_time).total_seconds() <= 60):
            return

    # ... build and send message as you already do ...


def _ist_target_today() -> datetime:
    now_ist = datetime.now(IST)
    return now_ist.replace(hour=HOUR, minute=MINUTES, second=0, microsecond=0)

def _ensure_daily_anchor(symbol: str):
    today_ist = _today_ist_str()
    target_ist = _ist_target_today()
    now_ist = datetime.now(IST)

    # Before scheduled time → do nothing
    if now_ist < target_ist:
        return

    if state[symbol].get("last_snapshot_date_ist") == today_ist:
        return

    # (Optional) After a grace window, still keep trying; or you can bail if you want:
    if (now_ist - target_ist).total_seconds() > SNAPSHOT_GRACE_SEC:
        print(f"[WARN] {symbol}: still no start price after grace; continuing to retry.")
        # no return → we keep retrying each loop

    snap = get_snapshot_at_ist_time(
        symbol=symbol,
        requested_date_ist=now_ist.date(),   # pin IST date
        server_timezone=SERVER_TZ,
        ist_hour=HOUR,
        ist_minute=MINUTES,
    )
    start_price = snap["anchors"]["price_at_anchor"]
    anchor_server_iso = snap["anchors"]["anchor_server"]
    anchor_ist_iso = snap["anchors"].get("anchor_ist")

    if start_price is None:
        print(f"[INFO] {symbol}: waiting for first bar at/after anchor "
              f"(server={anchor_server_iso}, ist={anchor_ist_iso})")
        return

    # Commit snapshot only when we have a real bar
    anchor_dt_srv = datetime.fromisoformat(anchor_server_iso)
    state[symbol].update({
        "start_price": start_price,
        "anchor_server_iso": anchor_server_iso,
        "anchor_dt_srv": anchor_dt_srv,
        "last_snapshot_date_ist": today_ist,
        "previous_executables": None,
    })
    print(f"\n=== {symbol} — snapshot @ {HOUR:02d}:{MINUTES:02d} IST ===")
    print("start_price:", start_price)
    print("anchor_server:", anchor_server_iso)
    if anchor_ist_iso: print("anchor_ist:", anchor_ist_iso)

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
    if not mt5.initialize():
        init_mt5("connected from main loop")

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
