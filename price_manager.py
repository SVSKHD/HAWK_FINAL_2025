from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable, Iterable, Set
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import time

# Reuse your APIs
from prices import (
    get_8am_snapshot,
    get_current_price,
    compute_target_server_dt,
    WeekendPolicy,
)

# --- Discord notifier (uses your notify module with limiter) ---
try:
    from notify import send_discord_message
except Exception:
    def send_discord_message(channel: str, message: str) -> bool:
        print(f"[Discord MOCK:{channel}] {message}")
        return True

# Defaults consistent with your code
SERVER_TZ = "Etc/GMT-3"           # broker/server tz
IST_TZ = "Asia/Kolkata"           # display/anchor check tz
ANCHOR_HOUR = 8                   # 08:00 server time → 10:30 IST
ANCHOR_MINUTE = 0
WEEKEND_POLICY: WeekendPolicy = "previous_trading_day"


@dataclass
class StartCache:
    symbol: str
    trading_date_server: date        # trading date of the anchor in server tz (after weekend shift)
    anchor_server_iso: str           # server dt at 08:00
    anchor_ist_iso: str              # IST dt at 10:30
    price_at_anchor: float
    # NEW: runner expects an 'anchors' mapping with specific keys
    anchors: Dict[str, object] = field(default_factory=dict)


class PriceManager:
    """
    Manages:
      - one-per-trading-day start price (8:00 server → 10:30 IST)
      - current price polling
    """

    def __init__(
        self,
        server_tz: str = SERVER_TZ,
        ist_tz: str = IST_TZ,
        weekend_policy: WeekendPolicy = WEEKEND_POLICY,
        anchor_hour: int = ANCHOR_HOUR,
        anchor_minute: int = ANCHOR_MINUTE,
    ):
        self.server_tz = server_tz
        self.ist_tz = ist_tz
        self.weekend_policy = weekend_policy
        self.anchor_hour = anchor_hour
        self.anchor_minute = anchor_minute
        self._start_cache: Dict[str, StartCache] = {}
        # ensure we only announce the daily roll once per trading day
        self._roll_notice_sent_for_date: Set[date] = set()

    # ---------- internal helpers ----------

    def _now_ist(self) -> datetime:
        return datetime.now(ZoneInfo(self.ist_tz))

    def _current_trading_day_server(self) -> date:
        """Return 'today shifted by weekend policy' in server tz (same logic as get_8am_snapshot)."""
        tz = ZoneInfo(self.server_tz)
        d = datetime.now(tz).date()
        target_srv, _ = compute_target_server_dt(
            requested_date=d,
            server_timezone=self.server_tz,
            hour=self.anchor_hour,
            minute=self.anchor_minute,
            weekend_policy=self.weekend_policy,
        )
        # target_srv is the anchor datetime for that date possibly shifted by weekend policy
        return target_srv.date()

    def _anchor_dt_server_today(self) -> datetime:
        """08:00 server dt for 'today' with weekend shift applied."""
        target_srv, _ = compute_target_server_dt(
            requested_date=None,
            server_timezone=self.server_tz,
            hour=self.anchor_hour,
            minute=self.anchor_minute,
            weekend_policy=self.weekend_policy,
        )
        return target_srv

    # ---------- public API ----------

    def get_start_price(self, symbol: str, *, force_refresh: bool = False) -> StartCache:
        """
        Get the start price (8:00 server) for the current trading day.
        Refreshes only once/day (unless force_refresh=True) and caches per symbol.
        """
        # If we have cache and it’s for current trading day, keep it
        current_trading_day = self._current_trading_day_server()
        cache = self._start_cache.get(symbol)

        if (not force_refresh) and cache and cache.trading_date_server == current_trading_day:
            return cache

        # Fetch fresh snapshot
        snap = get_8am_snapshot(
            symbol=symbol,
            requested_date=None,
            server_timezone=self.server_tz,
            ist_timezone=self.ist_tz,
            weekend_policy=self.weekend_policy,
        )

        price = snap["anchors"]["price_at_anchor"]
        if price is None:
            # as a defensive fallback, try forcing requested_date=today (still respects weekend shift)
            target_srv = self._anchor_dt_server_today()
            # NOTE: get_8am_snapshot will shift weekends internally as well
            snap = get_8am_snapshot(
                symbol=symbol,
                requested_date=target_srv.date(),
                server_timezone=self.server_tz,
                ist_timezone=self.ist_tz,
                weekend_policy=self.weekend_policy,
            )
            price = snap["anchors"]["price_at_anchor"]

        # Build anchors mapping the runner expects
        anchors = {
            "price_at_anchor": price,
            "start_price": price,                                # alias for compatibility
            "price": price,                                      # alias for compatibility
            "eight_am_server": snap["anchors"]["eight_am_server"],
            "eight_am_ist": snap["anchors"]["eight_am_ist"],
            # Names the runner/extremes helper expects:
            "anchor_dt_server": snap["anchors"]["eight_am_server"],
            "anchor_dt_ist": snap["anchors"]["eight_am_ist"],
        }

        cache = StartCache(
            symbol=symbol,
            trading_date_server=current_trading_day,
            anchor_server_iso=snap["anchors"]["eight_am_server"],
            anchor_ist_iso=snap["anchors"]["eight_am_ist"],
            price_at_anchor=price,
            anchors=anchors,
        )
        self._start_cache[symbol] = cache
        return cache

    def start_price_is_due_to_roll(self) -> bool:
        """
        Returns True if local IST time has passed today's anchor time (10:30 IST)
        AND our caches (if any) are from a previous trading day.
        """
        # anchor today (server 08:00 → IST)
        anchor_srv_today = self._anchor_dt_server_today()
        anchor_ist_today = anchor_srv_today.astimezone(ZoneInfo(self.ist_tz))
        now_ist = self._now_ist()

        if now_ist < anchor_ist_today:
            # not past the anchor yet → do not roll
            return False

        # after anchor → should roll (if any symbol cache exists and is old)
        today_trading = self._current_trading_day_server()
        for c in self._start_cache.values():
            if c.trading_date_server != today_trading:
                return True
        # If no caches yet, caller may choose to refresh on first request anyway.
        return False

    def maybe_refresh_all_start_prices(self, symbols: Iterable[str]) -> Dict[str, StartCache]:
        """
        If the daily anchor roll is due, refresh all provided symbols.
        Returns the (possibly updated) cache snapshot for those symbols.
        Also sends a one-time Discord 'info' message after a roll.
        """
        out: Dict[str, StartCache] = {}
        symbols = list(symbols)

        roll_due = self.start_price_is_due_to_roll()
        if roll_due:
            for s in symbols:
                out[s] = self.get_start_price(s, force_refresh=True)
        else:
            for s in symbols:
                out[s] = self.get_start_price(s, force_refresh=False)

        # Announce the roll once per trading day (after we actually refreshed)
        if roll_due:
            today_trading = self._current_trading_day_server()
            if today_trading not in self._roll_notice_sent_for_date:
                anchor_srv = self._anchor_dt_server_today()
                anchor_ist = anchor_srv.astimezone(ZoneInfo(self.ist_tz))

                # Compact per-symbol block of refreshed start prices
                lines = []
                for s in symbols:
                    try:
                        p = out[s].price_at_anchor
                        lines.append(f"- {s}: {p:.5f}" if isinstance(p, (int, float)) else f"- {s}: n/a")
                    except Exception:
                        pass
                sym_block = "\n".join(lines) if lines else "(no symbols)"

                send_discord_message(
                    "alert",
                    (
                        "🔄 **Daily Anchor Rolled** (10:30 IST)\n"
                        f"• Trading Day (server): **{today_trading.isoformat()}**\n"
                        f"• Anchor: **{anchor_srv.isoformat()} (server)** / **{anchor_ist.isoformat()} (IST)**\n"
                        f"• Start Prices:\n{sym_block}"
                    ),
                )
                self._roll_notice_sent_for_date.add(today_trading)

        return out

    def get_current_price(self, symbol: str) -> Dict[str, object]:
        """Thin wrapper around prices.get_current_price for consistency."""
        return get_current_price(symbol)

    def poll_current_prices(
        self,
        symbols: Iterable[str],
        interval_sec: int,
        on_tick: Optional[Callable[[str, Dict[str, object]], None]] = None,
        *,
        refresh_start_daily: bool = True,
    ) -> None:
        symbols = list(symbols)
        # Ensure we have start prices (cached) before polling
        self.maybe_refresh_all_start_prices(symbols)

        try:
            while True:
                if refresh_start_daily and self.start_price_is_due_to_roll():
                    self.maybe_refresh_all_start_prices(symbols)

                for s in symbols:
                    cur = self.get_current_price(s)
                    if on_tick:
                        on_tick(s, cur)
                time.sleep(interval_sec)
        except KeyboardInterrupt:
            # graceful exit
            return
