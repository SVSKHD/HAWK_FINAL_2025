from __future__ import annotations
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Iterable

# --- Config ---
SERVER_TZ = ZoneInfo("Etc/GMT-3")   # broker/server tz (UTC+3)
from pathlib import Path

# puts output next to your script as ...\historical_data\out
OUTPUT_DIR = Path(__file__).resolve().parent / "out"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SYMBOLS = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY"]  # NOTE: XAGUSD (silver). Your list had 'XAUGUSD'
DATE_START = "2025-10-01"   # server date (YYYY-MM-DD)
DATE_END   = "2025-10-31"   # server date (YYYY-MM-DD), inclusive

# --- Helpers ---

def mt5_init(login: Optional[int] = None, server: Optional[str] = None, password: Optional[str] = None) -> None:
    """
    Initialize MT5 and (optionally) login with explicit credentials.
    Raises RuntimeError on failure.
    """
    if not mt5.initialize():
        code, details = mt5.last_error()
        raise RuntimeError(f"MT5 initialize failed: {code} {details}")
    if all([login, server, password]):
        if not mt5.login(login=login, server=server, password=password):
            code, details = mt5.last_error()
            raise RuntimeError(f"MT5 login failed: {code} {details}")

def _ensure_symbol(sym: str) -> None:
    info = mt5.symbol_info(sym)
    if info is None:
        code, details = mt5.last_error()
        raise RuntimeError(f"symbol_info({sym}) failed: {code} {details}")
    if not info.visible and not mt5.symbol_select(sym, True):
        code, details = mt5.last_error()
        raise RuntimeError(f"symbol_select({sym}) failed: {code} {details}")

def _mt5_timeframe(code: str):
    code = code.upper()
    mapping = {
        "M1": mt5.TIMEFRAME_M1, "M2": mt5.TIMEFRAME_M2, "M3": mt5.TIMEFRAME_M3,
        "M4": mt5.TIMEFRAME_M4, "M5": mt5.TIMEFRAME_M5, "M10": mt5.TIMEFRAME_M10,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1,
    }
    return mapping.get(code, mt5.TIMEFRAME_M1)

def _copy_rates_range(sym: str, timeframe: str, day_start: datetime, day_end: datetime):
    tf = _mt5_timeframe(timeframe)
    return mt5.copy_rates_range(sym, tf, day_start, day_end)

def _server_day_range(server_date: datetime) -> tuple[datetime, datetime]:
    """Return [00:00, 23:59:59] in server tz for a given server date (tz-aware)."""
    d = server_date
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=SERVER_TZ)
    end   = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=SERVER_TZ)
    return start, end

def _daterange_inclusive(start_date: datetime, end_date: datetime):
    d = start_date
    while d <= end_date:
        yield d
        d += timedelta(days=1)

def _rates_to_df(rates) -> pd.DataFrame:
    """MT5 returns UTC seconds in 'time'. Convert to server tz and keep OHLC+tick_volume."""
    if rates is None or len(rates) == 0:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "tick_volume"])
    df = pd.DataFrame(rates)
    ts = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(SERVER_TZ)
    out = pd.DataFrame({
        "timestamp": ts,
        "open": df["open"],
        "high": df["high"],
        "low": df["low"],
        "close": df["close"],
        "tick_volume": df["tick_volume"],
    }).sort_values("timestamp", ignore_index=True)
    return out

def _resample_5m(df_m1: pd.DataFrame) -> pd.DataFrame:
    if df_m1.empty:
        return df_m1.copy()
    x = df_m1.set_index("timestamp")
    ohlc = x[["open","high","low","close"]].resample("5min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna()
    vol = x[["tick_volume"]].resample("5min").sum().reindex(ohlc.index, fill_value=0)
    out = ohlc.join(vol).reset_index()
    return out

# --- Main data exportors ---

def fetch_symbol_days_to_csv(symbol: str, start_date: str, end_date: str,
                             timeframe: str = "M1",
                             out_dir: Path = OUTPUT_DIR,
                             write_5m: bool = True) -> None:
    """
    Fetch each server-day for `symbol` between start_date..end_date (inclusive)
    and write CSVs:
      <out_dir>/<symbol>/raw_M1/<symbol>_YYYY-MM-DD_M1.csv
      <out_dir>/<symbol>/resampled_5m/<symbol>_YYYY-MM-DD_5m.csv  (if write_5m)
    """
    _ensure_symbol(symbol)
    out_raw = out_dir / symbol / "raw_M1"
    out_5m  = out_dir / symbol / "resampled_5m"
    out_raw.mkdir(parents=True, exist_ok=True)
    if write_5m:
        out_5m.mkdir(parents=True, exist_ok=True)

    sd = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=SERVER_TZ)
    ed = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=SERVER_TZ)

    for d in _daterange_inclusive(sd, ed):
        d0, d1 = _server_day_range(d)
        rates = _copy_rates_range(symbol, timeframe, d0, d1)
        df_m1 = _rates_to_df(rates)
        # write M1
        raw_path = out_raw / f"{symbol}_{d.date().isoformat()}_M1.csv"
        df_m1.to_csv(raw_path, index=False)
        # write 5m
        if write_5m:
            df_5m = _resample_5m(df_m1) if not df_m1.empty else df_m1.copy()
            five_path = out_5m / f"{symbol}_{d.date().isoformat()}_5m.csv"
            df_5m.to_csv(five_path, index=False)

def get_symbol_data_csv(symbols: Iterable[str] = SYMBOLS,
                        start_date: str = DATE_START,
                        end_date: str = DATE_END,
                        timeframe: str = "M1",
                        out_dir: Path = OUTPUT_DIR,
                        write_5m: bool = True) -> None:
    """
    Loop through symbols and export CSVs as per fetch_symbol_days_to_csv.
    """
    for sym in symbols:
        try:
            fetch_symbol_days_to_csv(sym, start_date, end_date, timeframe, out_dir, write_5m)
            print(f"[OK] {sym}: {start_date}..{end_date}")
        except Exception as e:
            print(f"[WARN] {sym}: {e}")

# --- Example CLI run ---
if __name__ == "__main__":
    try:
        # If your terminal is already logged in, you can omit credentials:
        mt5_init()  # or: mt5_init(login=1234567, server="YourBroker-Server", password="***")
        get_symbol_data_csv()
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass
