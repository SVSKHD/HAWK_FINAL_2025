# apps/bot-runner/src/config/symbols.py
from __future__ import annotations

from pydantic import BaseModel
from typing import Dict
from zoneinfo import ZoneInfo
from datetime import datetime

HOUR = 3
MINUTES = 30
SERVER_TZ = "Etc/GMT-3"
IST = ZoneInfo("Asia/Kolkata")
PROFIT_LIMIT_USD = 200.0       # stop after $200 realized profit in window
ASTRA_PREFIX = "Astra-"        # what your order comment starts with
WATCHDOG_FROM_UTC: datetime | None = None
_last_action: Dict[str, str] = {}


ENABLED_SYMBOLS = ["XAUUSD", "XAGUSD"]
NOTIFY_DELAY_SEC = 300   # 5 minutes → 09:05
SNAPSHOT_GRACE_SEC = 600 # optional: keep trying snapshot for 10 minutes after target

DRY_RUN = False # Toggle this to False for real trading

class SymbolConfig(BaseModel):
    symbol: str
    threshold_pips: int
    pip_size: float
    lot_size: float
    max_trades_per_day: int
    is_trade_able: bool


# Master dictionary for all supported symbols
SYMBOL_CONFIGS: Dict[str, SymbolConfig] = {
    "EURUSD": SymbolConfig(
        symbol="EURUSD",
        threshold_pips=15,
        pip_size=0.0001,
        lot_size=0.5,
        max_trades_per_day=6,
        is_trade_able=False,
    ),
    "GBPUSD": SymbolConfig(
        symbol="GBPUSD",
        threshold_pips=15,
        pip_size=0.0001,
        lot_size=0.5,
        max_trades_per_day=6,
        is_trade_able=False,
    ),
    "XAGUSD": SymbolConfig(
        symbol="XAGUSD",
        threshold_pips=300,
        pip_size=0.001,
        lot_size=0.5,
        max_trades_per_day=6,
        is_trade_able=False,
    ),
    "XAUUSD": SymbolConfig(
        symbol="XAUUSD",
        threshold_pips=400,
        pip_size=0.01,
        lot_size=0.5,
        max_trades_per_day=6,
        is_trade_able=True,
    ),
    "USDJPY": SymbolConfig(
        symbol="USDJPY",
        threshold_pips=20,
        pip_size=0.01,
        lot_size=0.5,
        max_trades_per_day=6,
        is_trade_able=True,
    ),
}
