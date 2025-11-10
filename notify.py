# notify.py — Discord notifier with RL + de-dup + retry (no Origin/Referer)

from __future__ import annotations
import os, json, re, time, hashlib
from collections import defaultdict, deque
from typing import Deque, DefaultDict, Dict, Tuple
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

WEBHOOK_RE = re.compile(
    r"^https://(ptb\.|canary\.)?discord\.com/api/webhooks/\d+/[A-Za-z0-9_\-\.]+$"
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _sanitize_url(v: str | None) -> str:
    if not v:
        return ""
    s = v.strip().strip('"').strip("'").strip()
    if s and s[-1] in ".,;":
        s = s[:-1]
    return s

def _get_env_any(names: list[str]) -> str:
    for n in names:
        val = _sanitize_url(os.getenv(n))
        if val:
            return val
    return ""

def _get_webhooks() -> Dict[str, str]:
    info = _get_env_any([
        "DISCORD_WEBHOOK_INFO", "DISCORD_INFO_WEBHOOK",
        "DISCORD_WEBHOOK_NORMAL", "DISCORD_NORMAL_WEBHOOK",
    ])
    alert = _get_env_any([
        "DISCORD_WEBHOOK_ALERT", "DISCORD_ALERT_WEBHOOK",
        "DISCORD_WEBHOOK_CRITICAL", "DISCORD_CRITICAL_WEBHOOK",
        "DISCORD_WEBHOOK_INFO", "DISCORD_INFO_WEBHOOK",
    ])
    critical = _get_env_any([
        "DISCORD_WEBHOOK_CRITICAL", "DISCORD_CRITICAL_WEBHOOK",
        "DISCORD_WEBHOOK_ALERT", "DISCORD_ALERT_WEBHOOK",
    ])
    normal = _get_env_any([
        "DISCORD_WEBHOOK_NORMAL", "DISCORD_NORMAL_WEBHOOK",
        "DISCORD_WEBHOOK_INFO", "DISCORD_INFO_WEBHOOK",
    ])
    trade = _get_env_any([
        "DISCORD_WEBHOOK_TRADE", "DISCORD_TRADE_WEBHOOK",
        "DISCORD_WEBHOOK_INFO", "DISCORD_INFO_WEBHOOK",
        "DISCORD_WEBHOOK_NORMAL", "DISCORD_NORMAL_WEBHOOK",
    ])
    return {"info": info, "alert": alert, "critical": critical, "normal": normal, "trade": trade}

def _validate_webhook(url: str) -> bool:
    return bool(url and WEBHOOK_RE.match(url))

def _clean_message(v: str) -> str:
    v = (v or "").strip()
    if v and v[-1] in " .;,":
        v = v[:-1]
    return v

class RateLimiter:
    def __init__(self, max_per_window: int, window_sec: int, cooldown_sec: int):
        self.max_per_window = int(max_per_window)
        self.window_sec = int(window_sec)
        self.cooldown_sec = int(cooldown_sec)
        self._events: DefaultDict[str, Deque[float]] = defaultdict(deque)
        self._last_sent: Dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        dq = self._events[key]
        while dq and now - dq[0] > self.window_sec:
            dq.popleft()
        last = self._last_sent.get(key, 0.0)
        if now - last < self.cooldown_sec:
            return False
        if len(dq) >= self.max_per_window:
            return False
        dq.append(now)
        self._last_sent[key] = now
        return True

class DeDuper:
    def __init__(self, ttl_sec: int):
        self.ttl = int(ttl_sec)
        self._store: Dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        for k, ts in list(self._store.items()):
            if now - ts > self.ttl:
                del self._store[k]
        if key in self._store:
            return False
        self._store[key] = now
        return True

def _dedup_key(channel: str, message: str) -> str:
    payload = " ".join((message or "").split()).lower()
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{channel}:{h}"

GLOBAL_MAX_PER_WINDOW = _int_env("DISCORD_RATE_MAX_PER_WINDOW", 5)
GLOBAL_WINDOW_SEC     = _int_env("DISCORD_RATE_WINDOW_SEC", 60)
GLOBAL_COOLDOWN_SEC   = _int_env("DISCORD_RATE_COOLDOWN_SEC", 15)
GLOBAL_DEDUP_TTL_SEC  = _int_env("DISCORD_DEDUP_TTL_SEC", 45)

def _chan_cfg(chan: str) -> Tuple[int, int, int]:
    pfx = f"DISCORD_{chan.upper()}"
    return (
        _int_env(f"{pfx}_MAX_PER_WINDOW", GLOBAL_MAX_PER_WINDOW),
        _int_env(f"{pfx}_WINDOW_SEC",     GLOBAL_WINDOW_SEC),
        _int_env(f"{pfx}_COOLDOWN_SEC",   GLOBAL_COOLDOWN_SEC),
    )

_RL: Dict[str, RateLimiter] = {}
def _rl_for_channel(chan: str) -> RateLimiter:
    if chan not in _RL:
        mpw, win, cd = _chan_cfg(chan)
        _RL[chan] = RateLimiter(mpw, win, cd)
    return _RL[chan]

_DD = DeDuper(ttl_sec=GLOBAL_DEDUP_TTL_SEC)

def _headers() -> Dict[str, str]:
    # Do NOT send Origin/Referer for webhooks; Discord will reject (code 50067).
    return {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
    }

def _post(url: str, payload: Dict[str, object], *, retries: int = 2, backoff: float = 0.7) -> bool:
    body = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        attempt += 1
        req = Request(url, data=body, headers=_headers(), method="POST")
        try:
            with urlopen(req, timeout=10) as resp:
                status = getattr(resp, "status", 200)
                cf_ray = getattr(resp, "headers", {}).get("CF-Ray", "")
                print(f"[Discord] Sent (status={status}) CF-Ray={cf_ray or '-'}")
                return 200 <= int(status) < 300
        except HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                err_body = ""
            cf_ray = getattr(e, "headers", {}).get("CF-Ray", "")
            print(f"[Discord] HTTPError {e.code} CF-Ray={cf_ray or '-'}: {err_body.strip()[:300]}")
            if attempt <= retries and e.code in (403, 429, 520, 525):
                time.sleep(backoff * attempt)
                continue
            return False
        except URLError as e:
            print(f"[Discord] URLError: {e.reason}")
            if attempt <= retries:
                time.sleep(backoff * attempt)
                continue
            return False
        except Exception as e:
            print(f"[Discord] Unknown error: {e}")
            if attempt <= retries:
                time.sleep(backoff * attempt)
                continue
            return False

def send_discord_message(channel: str, message: str) -> bool:
    """
    Send a plain-text Discord message.
    Valid channels: 'info' | 'alert' | 'critical' | 'trade' | 'normal'
    Unknown channel names fall back to 'info'.
    """
    channel = (channel or "info").strip().lower()
    if channel not in ("info", "alert", "critical", "trade", "normal"):
        channel = "info"

    webhooks = _get_webhooks()
    url = _sanitize_url(webhooks.get(channel) or webhooks.get("info") or webhooks.get("normal"))

    if not _validate_webhook(url):
        print(f"[Discord] Missing/invalid webhook for channel='{channel}'. Skipping.")
        return False

    message = _clean_message(message)
    if not message:
        return False

    if not _rl_for_channel(channel).allow(channel):
        print(f"[Discord] Rate-limited on channel='{channel}'. Dropped.")
        return False

    if not _DD.allow(_dedup_key(channel, message)):
        print(f"[Discord] De-dup suppressed for channel='{channel}'.")
        return False

    return _post(url, {"content": message})

# if __name__ == "__main__":
#     send_discord_message("info", "✅ notify.py: removed Origin/Referer; should fix 50067.")
#     send_discord_message("alert", "✅ notify.py: removed Origin/Referer; should fix 50067.")
#     send_discord_message("normal", "✅ notify.py: removed Origin/Referer; should fix 50067.")
#     send_discord_message("critical", "✅ notify.py: removed Origin/Referer; should fix 50067.")
