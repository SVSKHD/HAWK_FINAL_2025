# main_runner.py
from datetime import datetime
from zoneinfo import ZoneInfo
import time
from typing import Dict, Any, Optional

from config import SERVER_TZ, IST, ENABLED_SYMBOLS, HOUR, MINUTES
from prices import get_snapshot_at_ist_time, get_current_price, get_high_low_since_anchor
from executor import execute_place_and_close
from notify import send_discord_message  # <-- NEW

# Toggle this to False for real trading
DRY_RUN = True

state: Dict[str, Dict[str, Any]] = {
    s: {
        "anchor_server_iso": None,
        "anchor_dt_srv": None,
        "start_price": None,
        "previous_executables": None,
        "last_snapshot_date_ist": None,
        "start_notified_date_ist": None,   # <-- NEW (avoid duplicate daily notify)
    } for s in ENABLED_SYMBOLS
}

def _today_ist_str() -> str:
    return datetime.now(IST).date().isoformat()

def _ensure_daily_anchor(symbol: str):
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

    # --- Notify INFO channel once per IST day ---
    try:
        # current tick
        tick = get_current_price(symbol)
        current = None
        current_time = None
        if isinstance(tick, dict):
            current = float(tick.get("last") or tick.get("bid") or tick.get("ask"))
            current_time = tick.get("time")

        # highs/lows since anchor
        hl = get_high_low_since_anchor(symbol, anchor_dt_srv)
        high: Optional[float] = hl.high if hl and hl.high is not None else current
        low: Optional[float]  = hl.low  if hl and hl.low  is not None else current

        # de-dup per day
        if state[symbol]["start_notified_date_ist"] != today:
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

    except Exception as e:
        print(f"[WARN] notify failed for {symbol}: {e}")

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
    while True:
        try:
            for s in ENABLED_SYMBOLS:
                _ensure_daily_anchor(s)
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
