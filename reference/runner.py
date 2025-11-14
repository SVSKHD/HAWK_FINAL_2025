# runner.py — fixed & instrumented (handles dict/float current_price) + 10:30 IST refresher
from __future__ import annotations

import time
import json
from dataclasses import asdict, is_dataclass
from typing import Iterable, Optional, Callable, Dict, Any
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo  # NEW

from price_manager import PriceManager
from prices import get_extremes_relative_to_price
from common_logic import PriceComponent
from threshold_logic import evaluate_threshold, execute_threshold_decision
from mt5 import init_mt5
from notify import send_discord_message

try:
    from config import SYMBOL_CONFIGS  # expects keys: threshold_pips, pip_size
except Exception:
    SYMBOL_CONFIGS = {}

# ---------- helpers ----------
def _normalize_anchor_dict(raw_start: Any) -> Dict[str, Any]:
    if raw_start is None:
        return {}
    if isinstance(raw_start, Mapping):
        return dict(raw_start.get("anchors") or raw_start)
    if is_dataclass(raw_start):
        data = asdict(raw_start)
        return dict(data.get("anchors") or data)
    maybe_anchors = getattr(raw_start, "anchors", None)
    if isinstance(maybe_anchors, Mapping):
        return dict(maybe_anchors)
    return {
        "price_at_anchor": getattr(raw_start, "price_at_anchor", None),
        "start_price": getattr(raw_start, "start_price", None),
        "price": getattr(raw_start, "price", None),
        "anchor_dt_server": getattr(raw_start, "anchor_dt_server", None),
        "anchor_dt_ist": getattr(raw_start, "anchor_dt_ist", None),
    }

def _resolve_start_price(anchors: Dict[str, Any]) -> Optional[float]:
    sp = anchors.get("price_at_anchor") or anchors.get("start_price") or anchors.get("price")
    try:
        return float(sp) if sp is not None else None
    except (TypeError, ValueError):
        return None

def _extract_price(val: Any) -> Optional[float]:
    """Accept float or dict with bid/ask/last/price/mid (string or number)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        bid = val.get("bid"); ask = val.get("ask")
        mid = val.get("mid"); price = val.get("price"); last = val.get("last")
        # prefer mid from bid/ask
        try:
            if bid is not None and ask is not None:
                b = float(bid); a = float(ask)
                if b > 0 and a > 0:
                    return (a + b) / 2.0
        except Exception:
            pass
        # then explicit mid/price/last (numbers or strings)
        for k in (mid, price, last, bid, ask):
            if k is None:
                continue
            try:
                f = float(k)
                if f > 0:
                    return f
            except Exception:
                continue
    return None

def _compute_threshold_ratio(symbol: str, pc: PriceComponent) -> Optional[float]:
    existing = getattr(pc, "threshold_ratio", None)
    if isinstance(existing, (int, float)):
        return float(existing)
    cfg = SYMBOL_CONFIGS.get(symbol) or {}
    threshold_pips = cfg.get("threshold_pips")
    pip_size = cfg.get("pip_size")
    if not threshold_pips or not pip_size:
        return None
    try:
        moved_pips = abs(float(pc.current_price) - float(pc.start_price)) / float(pip_size)
        return moved_pips / float(threshold_pips)
    except Exception:
        return None

def _stage_from_ratio(ratio: Optional[float]) -> int:
    if ratio is None or ratio < 0:
        return 0
    return int(ratio)

def _send_snapshot(symbols: Iterable[str], pm: PriceManager, *, label: str) -> None:
    """Send a structured snapshot to Discord (info) and save JSON to bot_logs/YYYY-MM-DD/."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    snapshot = []
    lines = [f"🟢 **{label} Snapshot** — {now_iso}", ""]

    for s in symbols:
        raw = pm.get_start_price(s)
        anchors = _normalize_anchor_dict(raw)
        start_price = _resolve_start_price(anchors)

        cur_raw = pm.get_current_price(s)  # may be float or dict in your repo
        current_price = _extract_price(cur_raw)

        snapshot.append({
            "symbol": s,
            "start_price": start_price,
            "current_price": current_price,
            "timestamp": now_iso,
        })
        lines.append(f"{s}: start={start_price}  current={current_price}")

    send_discord_message("info", "\n".join(lines))

    date_str = datetime.now().strftime("%Y-%m-%d")
    folder = Path("bot_logs") / date_str
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{label.lower().replace(' ', '_')}_snapshot.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

# ---------- 10:30 IST special refresh ----------
def _send_1030_refresh_if_due(symbols: Iterable[str], pm: PriceManager, *, logs_root: Path = Path("bot_logs")) -> None:
    """
    One-time message between 10:30:00–10:30:59 IST with a compact snapshot.
    Uses bot_logs/YYYY-MM-DD/.1030_sent as an idempotency marker.
    """
    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(tz=ist)
    # only act inside the 10:30 minute
    if not (now_ist.hour == 10 and now_ist.minute == 30):
        return

    day_dir = logs_root / now_ist.date().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    marker = day_dir / ".1030_sent"
    if marker.exists():
        return

    lines = [f"⚡ **10:30 Refresh** — {now_ist.isoformat(timespec='seconds')} IST", ""]
    for s in symbols:
        # start price via your existing helpers/PM
        raw_start = pm.get_start_price(s)
        anchors = _normalize_anchor_dict(raw_start)
        sp = _resolve_start_price(anchors)

        # current price via PM (accept dict/float)
        cur_raw = pm.get_current_price(s)
        cp = _extract_price(cur_raw)

        # threshold ratio using your existing calculator
        try:
            pc_temp = PriceComponent(
                symbol=s,
                start_price=float(sp) if sp is not None else 0.0,
                current_price=float(cp) if cp is not None else 0.0,
                latest_high=float(cp) if cp is not None else 0.0,
                latest_low=float(cp) if cp is not None else 0.0,
            )
            ratio = _compute_threshold_ratio(s, pc_temp)
        except Exception:
            ratio = None

        sp_txt = f"{sp:.5f}" if isinstance(sp, (int, float)) and sp is not None else "n/a"
        cp_txt = f"{cp:.5f}" if isinstance(cp, (int, float)) and cp is not None else "n/a"
        ratio_txt = f"{ratio:.2f}x" if isinstance(ratio, (int, float)) else "n/a"
        lines.append(f"• {s}: start {sp_txt} → now {cp_txt} · ratio **{ratio_txt}**")

    try:
        send_discord_message("info", "\n".join(lines))
    finally:
        # Write the marker regardless, to guarantee once-per-day behavior.
        marker.write_text("sent", encoding="utf-8")

# ---------- main ----------
def run(
    symbols: Iterable[str],
    *,
    interval_sec: float = 1.0,
    server_tz: str = "Etc/GMT-3",
    ist_tz: str = "Asia/Kolkata",
    refresh_start_daily: bool = True,
    on_tick: Optional[Callable[[str, float], None]] = None,
    on_decision: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    on_threshold_hit: Optional[Callable[[str, float], None]] = None,
) -> None:
    init_mt5("runner.py")
    pm = PriceManager(server_tz=server_tz, ist_tz=ist_tz)
    pm.maybe_refresh_all_start_prices(symbols)

    # Boot snapshot → Discord + file
    _send_snapshot(symbols, pm, label="BOT-2025 Boot")

    last_stage_notified: Dict[str, int] = defaultdict(int)

    try:
        while True:
            # Daily rollover
            if refresh_start_daily and pm.start_price_is_due_to_roll():
                pm.maybe_refresh_all_start_prices(symbols)
                for sym in symbols:
                    last_stage_notified[sym] = 0
                _send_snapshot(symbols, pm, label="Daily Anchor Rollover")

            for s in symbols:
                # --- current price (robust)
                cur_raw = pm.get_current_price(s)  # float OR dict in your codebase
                cur_f = _extract_price(cur_raw)
                if cur_f is None:
                    send_discord_message("alert", f"[{s}] No usable price (tick missing). Skipping this tick.")
                    continue

                if on_tick:
                    on_tick(s, cur_f)

                # --- start/anchors
                raw_start = pm.get_start_price(s)
                anchors = _normalize_anchor_dict(raw_start)
                start_price = _resolve_start_price(anchors)
                if start_price is None:
                    pm.maybe_refresh_all_start_prices([s])
                    continue

                # --- extremes (best-effort)
                latest_high = cur_f
                latest_low = cur_f
                try:
                    since_srv_iso = anchors.get("anchor_dt_server")
                    if since_srv_iso:
                        extremes = get_extremes_relative_to_price(s, start_price, since_srv_iso)
                        if isinstance(extremes, Mapping):
                            if extremes.get("highest_above") is not None:
                                latest_high = float(extremes["highest_above"])
                            if extremes.get("lowest_below") is not None:
                                latest_low = float(extremes["lowest_below"])
                except Exception:
                    pass

                # --- component
                pc = PriceComponent(
                    symbol=s,
                    start_price=float(start_price),
                    current_price=cur_f,
                    latest_high=latest_high,
                    latest_low=latest_low,
                )

                # --- threshold stage handling
                ratio = _compute_threshold_ratio(s, pc)
                stage_now = _stage_from_ratio(ratio)
                stage_prev = last_stage_notified[s]
                if stage_now >= 1 and stage_now > stage_prev:
                    data = pc.as_dict() if hasattr(pc, "as_dict") else {}
                    msg = (
                        f"[{s}] threshold x{stage_now} hit | "
                        f"start={data.get('start_price', pc.start_price)} "
                        f"cur={data.get('current_price', pc.current_price)} "
                        f"pips={data.get('pips_moved', 'n/a')} "
                        f"dir={data.get('direction', 'n/a')} "
                        f"strong={data.get('strong_direction', 'n/a')} "
                        f"ratio={data.get('threshold_ratio', round(ratio, 2) if ratio is not None else 'n/a')}"
                    )
                    send_discord_message("info", msg)
                    last_stage_notified[s] = stage_now
                    if on_threshold_hit:
                        on_threshold_hit(s, float(ratio) if ratio is not None else float(stage_now))

                # --- open positions?
                try:
                    import MetaTrader5 as mt5  # type: ignore
                    is_open = bool(mt5.positions_get(symbol=s) or ())
                except Exception:
                    is_open = False

                # --- decision & execution
                decision = evaluate_threshold(pc, is_position_open=is_open)
                if on_decision:
                    on_decision(s, getattr(decision, "__dict__", {"signal": str(decision)}))
                execute_threshold_decision(decision)

            # 10:30 IST special refresh (once per day)
            _send_1030_refresh_if_due(symbols, pm, logs_root=Path("bot_logs"))

            time.sleep(interval_sec)

    except KeyboardInterrupt:
        print("[Runner] Interrupted by user, shutting down.")
        return
