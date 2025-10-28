import os, json, re
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

WEBHOOK_RE = re.compile(
    r"^https://(ptb\.|canary\.)?discord\.com/api/webhooks/\d+/[A-Za-z0-9_\-\.]+$"
)

def _clean(val: str | None) -> str | None:
    if not val:
        return None
    # strip whitespace and wrapping quotes
    v = val.strip().strip('"').strip("'")
    # common paste mistake: trailing punctuation or spaces
    while v and v[-1] in " .;,":
        v = v[:-1]
    return v

def get_urls() -> dict[str, str | None]:
    normal   = _clean(os.getenv("DISCORD_WEBHOOK_NORMAL"))
    info     = _clean(os.getenv("DISCORD_WEBHOOK_INFO"))
    critical = _clean(os.getenv("DISCORD_WEBHOOK_CRITICAL"))
    return {"normal": normal, "info": info, "critical": critical}

def validate_webhook(url: str) -> bool:
    return bool(url and WEBHOOK_RE.match(url))

def send_discord_message(channel: str, message: str) -> bool:
    urls = get_urls()
    url = urls.get(channel)
    if not url:
        print(f"[Discord] No URL configured for channel '{channel}'")
        return False
    if not validate_webhook(url):
        print(f"[Discord] URL for '{channel}' doesn’t look valid: {url!r}")
        return False

    payload = {"content": message}
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "bot-2025/1.0"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            status = getattr(resp, "status", 200)
            print(f"[Discord] Sent (status={status})")
            return 200 <= int(status) < 300
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"[Discord] HTTPError {e.code}: {body}")
        return False
    except URLError as e:
        print(f"[Discord] URLError: {e.reason}")
        return False
    except Exception as e:
        print(f"[Discord] Unknown error: {e}")
        return False



