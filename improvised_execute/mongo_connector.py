# mongo_connector.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import os
from pymongo import MongoClient, ASCENDING
from pymongo.collection import Collection
from pymongo.errors import PyMongoError


# ==========================
#   ENV / CONFIG
# ==========================

# Example:
#   MONGO_URI="mongodb+srv://user:pass@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority"
#   MONGO_DB="astra_bot"
#   MONGO_STATE_COLL="daily_state"
#   MONGO_EVENTS_COLL="trade_events"

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB", "astra_bot")
MONGO_STATE_COLL_NAME = os.getenv("MONGO_STATE_COLL", "daily_state")
MONGO_EVENTS_COLL_NAME = os.getenv("MONGO_EVENTS_COLL", "trade_events")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_utc_str() -> str:
    return _now_utc().date().isoformat()  # "YYYY-MM-DD"


# ==========================
#   MongoState CLASS
# ==========================

@dataclass
class MongoState:
    """
    Simple Mongo wrapper for:
      - Daily state document (one per UTC date)
      - Per-trade / per-event log

    Daily state schema (daily_state collection):
      {
        _id: "YYYY-MM-DD",        # date key (UTC)
        date: "YYYY-MM-DD",
        locked: bool,
        lock_reason: str | None,
        max_total_pnl: float,
        created_at: datetime,
        updated_at: datetime,
      }

    Trade event schema (trade_events collection):
      {
        _id: ObjectId,
        date: "YYYY-MM-DD",
        ts: str,                 # ISO timestamp from executor
        symbol: str,
        event: str,
        action: str | None,
        direction: str | None,
        total_pnl: float | None,
        trade_response: dict | None,
        extra: dict | None,
        created_at: datetime,
      }
    """

    uri: str = field(default_factory=lambda: MONGO_URI)
    db_name: str = field(default_factory=lambda: MONGO_DB_NAME)
    state_coll_name: str = field(default_factory=lambda: MONGO_STATE_COLL_NAME)
    events_coll_name: str = field(default_factory=lambda: MONGO_EVENTS_COLL_NAME)

    _client: Optional[MongoClient] = field(init=False, default=None)
    _state_coll: Optional[Collection] = field(init=False, default=None)
    _events_coll: Optional[Collection] = field(init=False, default=None)
    _available: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if not self.uri:
            print("[MongoState] No MONGO_URI set; Mongo features disabled.")
            self._available = False
            return

        try:
            self._client = MongoClient(self.uri)
            db = self._client[self.db_name]
            self._state_coll = db[self.state_coll_name]
            self._events_coll = db[self.events_coll_name]

            # Simple indexes (not mandatory but nice to have)
            self._state_coll.create_index([("date", ASCENDING)], unique=True)
            self._events_coll.create_index([("date", ASCENDING), ("ts", ASCENDING)])

            self._available = True
            print(f"[MongoState] Connected to {self.db_name} at {self.uri}")
        except PyMongoError as e:
            print(f"[MongoState] Mongo connection failed: {e}")
            self._available = False

    # --------------- internal helpers ---------------

    def _default_state_for_date(self, date_str: str) -> Dict[str, Any]:
        now = _now_utc()
        return {
            "_id": date_str,
            "date": date_str,
            "locked": False,
            "lock_reason": None,
            "max_total_pnl": 0.0,
            "created_at": now,
            "updated_at": now,
        }

    # --------------- PUBLIC API ---------------

    def get_today_state(self) -> Dict[str, Any]:
        """
        Load today's state (UTC date). If missing, create a default doc.
        """
        date_str = _today_utc_str()

        # If Mongo not available, return in-memory default
        if not self._available or self._state_coll is None:
            print("[MongoState] get_today_state in fallback (no Mongo connection).")
            return self._default_state_for_date(date_str)

        try:
            doc = self._state_coll.find_one({"_id": date_str})
            if doc is None:
                # Insert default doc
                doc = self._default_state_for_date(date_str)
                self._state_coll.insert_one(doc)
                print(f"[MongoState] Created new daily_state doc for {date_str}")
            return doc
        except PyMongoError as e:
            print(f"[MongoState] get_today_state error: {e}")
            return self._default_state_for_date(date_str)

    def update_today_state(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply partial update to today's state doc and return updated doc.
        """
        date_str = _today_utc_str()
        now = _now_utc()

        if not self._available or self._state_coll is None:
            print("[MongoState] update_today_state in fallback (no Mongo connection).")
            base = self._default_state_for_date(date_str)
            base.update(patch)
            base["updated_at"] = now
            return base

        try:
            # Use $setOnInsert for defaults, $set for patch+updated_at
            defaults = self._default_state_for_date(date_str)
            # Remove _id from defaults for upsert
            defaults.pop("_id", None)

            self._state_coll.update_one(
                {"_id": date_str},
                {
                    "$setOnInsert": defaults,
                    "$set": {**patch, "updated_at": now},
                },
                upsert=True,
            )
            doc = self._state_coll.find_one({"_id": date_str})
            return doc if doc else self._default_state_for_date(date_str)
        except PyMongoError as e:
            print(f"[MongoState] update_today_state error: {e}")
            base = self._default_state_for_date(date_str)
            base.update(patch)
            base["updated_at"] = now
            return base

    def append_trade_event(self, event: Dict[str, Any]) -> None:
        """
        Log a trade / action event.

        The executor normally passes:
          {
            "ts": <iso>,
            "symbol": ...,
            "event": ...,
            "action": ...,
            "direction": ...,
            "total_pnl": ...,
            "trade_response": ...,
          }
        """
        date_str = _today_utc_str()
        now = _now_utc()

        doc = {
            "date": date_str,
            "created_at": now,
        }
        doc.update(event or {})

        if "ts" not in doc:
            doc["ts"] = now.isoformat(timespec="seconds")

        if not self._available or self._events_coll is None:
            print(f"[MongoState] (fallback) trade_event: {doc}")
            return

        try:
            self._events_coll.insert_one(doc)
        except PyMongoError as e:
            print(f"[MongoState] append_trade_event error: {e}")


# Singleton used by executor.py
STATE = MongoState()


if __name__ == "__main__":
    # Quick self-test
    st = STATE.get_today_state()
    print("today_state:", st)

    STATE.append_trade_event({
        "ts": _now_utc().isoformat(timespec="seconds"),
        "symbol": "XAUUSD",
        "event": "self_test",
        "action": "none",
        "direction": None,
        "total_pnl": 0.0,
    })

    st2 = STATE.update_today_state({"locked": False, "lock_reason": None})
    print("updated_state:", st2)
