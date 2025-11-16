# mongo_state.py
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from pymongo import MongoClient
from config import IST


def _today_ist_str() -> str:
    """IST calendar date string, e.g. '2025-11-16'."""
    return datetime.now(IST).date().isoformat()


class MongoState:
    """
    Mongo helper:
      • Threshold state (1x / 2x timestamps) per symbol per IST day
      • Profit-halt state per IST day

    Collections:
      - threshold_coll:  { symbol, date_ist, first_threshold_reached_at, second_threshold_reached_at, ... }
      - watchdog_coll:   { date_ist, halted, total_pnl, limit_usd, ... }
    """

    def __init__(self) -> None:
        uri = os.getenv("MONGO_ATLAS_URI")
        if not uri:
            raise RuntimeError("MONGO_ATLAS_URI is not set in environment")

        client = MongoClient(uri)
        db_name = os.getenv("MONGO_DB_NAME", "astra_bot")
        db = client[db_name]

        self.threshold_coll = db[os.getenv("MONGO_THRESH_COLLECTION", "threshold_state")]
        self.watchdog_coll = db[os.getenv("MONGO_WATCHDOG_COLLECTION", "watchdog_state")]

        # Optional but nice: ensure indexes
        self.threshold_coll.create_index([("symbol", 1), ("date_ist", 1)], unique=True)
        self.watchdog_coll.create_index([("date_ist", 1)], unique=True)

    # ---------- Threshold state (first / second) ----------

    def upsert_threshold_state(
        self,
        symbol: str,
        *,
        first_threshold_reached_at: Optional[str],
        second_threshold_reached_at: Optional[str],
    ) -> None:
        """
        Store latest first/second threshold timestamps for (symbol, today_ist).
        Idempotent per day.
        """
        date_ist = _today_ist_str()
        doc = {
            "symbol": symbol,
            "date_ist": date_ist,
            "first_threshold_reached_at": first_threshold_reached_at,
            "second_threshold_reached_at": second_threshold_reached_at,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.threshold_coll.update_one(
            {"symbol": symbol, "date_ist": date_ist},
            {"$set": doc},
            upsert=True,
        )

    def load_threshold_state(self, symbol: str) -> Dict[str, Optional[str]]:
        """
        Return today's stored threshold timestamps for this symbol.
        """
        date_ist = _today_ist_str()
        doc = self.threshold_coll.find_one({"symbol": symbol, "date_ist": date_ist})
        if not doc:
            return {
                "first_threshold_reached_at": None,
                "second_threshold_reached_at": None,
            }
        return {
            "first_threshold_reached_at": doc.get("first_threshold_reached_at"),
            "second_threshold_reached_at": doc.get("second_threshold_reached_at"),
        }

    # ---------- Profit watchdog / halt state ----------

    def set_profit_halt(
        self,
        *,
        total_pnl: float,
        limit_usd: float,
        note: Optional[str] = None,
    ) -> None:
        """
        Mark that for *today* the profit limit was hit and trading must be halted.
        """
        date_ist = _today_ist_str()
        doc = {
            "date_ist": date_ist,
            "halted": True,
            "total_pnl": float(total_pnl),
            "limit_usd": float(limit_usd),
            "note": note or "",
            "triggered_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.watchdog_coll.update_one(
            {"date_ist": date_ist},
            {"$set": doc},
            upsert=True,
        )

    def get_profit_halt_today(self) -> Optional[Dict[str, Any]]:
        """
        Check if today's trading is already halted (from previous process run).
        """
        date_ist = _today_ist_str()
        return self.watchdog_coll.find_one({"date_ist": date_ist, "halted": True})


# Global singleton
STATE = MongoState()
