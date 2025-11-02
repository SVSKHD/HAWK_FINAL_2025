
from __future__ import annotations
import os, json, sys, pandas as pd
from dataclasses import dataclass
from typing import Dict, Tuple, Any, List, Optional
from zoneinfo import ZoneInfo
from datetime import datetime

SERVER_TZ = ZoneInfo("Etc/GMT-3")

@dataclass
class SymbolConfig:
    symbol: str
    threshold_pips: float
    pip_size: float
    lot_size: float = 0.5
    pip_value_per_lot: float = 10.0
    t2_multiple: float = 2.0
    spike_tolerance: float = 1.25
    hedge_min_move_pips: float = 5.0
    hedge_close_pref: float = 25.0
    hedge_close_spike: float = 50.0

@dataclass
class Trade:
    day: str
    symbol: str
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    lot: float
    t1_level: float
    t2_level: float
    start_price: float

@dataclass
class Hedge:
    open_time: pd.Timestamp
    entry_price: float
    lot: float
    direction: str

def pips_between(a: float, b: float, pip_size: float) -> float:
    return (b - a) / pip_size

def price_from_pips(base: float, pips: float, pip_size: float) -> float:
    return base + pips * pip_size

def pnl_usd(price_diff: float, pip_size: float, lot: float, pip_value_per_lot: float) -> float:
    pips = price_diff / pip_size
    return pips * pip_value_per_lot * lot

class ThresholdHedgeBacktester:
    def __init__(self, cfg: SymbolConfig):
        self.cfg = cfg

    def _get_day_anchor_price(self, df: pd.DataFrame):
        day = df.index[0].date()
        start_ts = pd.Timestamp(year=day.year, month=day.month, day=day.day, tz=SERVER_TZ)
        exact = df.loc[df.index == start_ts]
        if len(exact):
            return start_ts, float(exact["price"].iloc[0])
        window = df.between_time("00:00", "00:05", include_start=True, include_end=True)
        if len(window):
            return window.index[0], float(window["price"].iloc[0])
        return None

    def run_day(self, day_df: pd.DataFrame) -> Dict[str, Any]:
        cfg = self.cfg
        day = day_df.index[0].date().isoformat()
        anchor = self._get_day_anchor_price(day_df)
        if not anchor:
            return {"day": day, "summary": {"reason": "no anchor"}, "trades": [], "events": []}
        anchor_time, start_price = anchor
        t1 = cfg.threshold_pips
        t2 = t1 * cfg.t2_multiple
        up_t1 = price_from_pips(start_price, +t1, cfg.pip_size)
        up_t2 = price_from_pips(start_price, +t2, cfg.pip_size)
        dn_t1 = price_from_pips(start_price, -t1, cfg.pip_size)
        dn_t2 = price_from_pips(start_price, -t2, cfg.pip_size)

        open_trade: Trade | None = None
        open_hedge: Hedge | None = None
        trade_logs: List[Dict[str, Any]] = []

        for ts, row in day_df.iterrows():
            price = float(row["price"])
            if open_trade is None:
                if price >= up_t1 and price <= price_from_pips(start_price, t1 * cfg.spike_tolerance, cfg.pip_size):
                    open_trade = Trade(day, cfg.symbol, "long", ts, price, cfg.lot_size, up_t1, up_t2, start_price)
                    continue
                if price <= dn_t1 and price >= price_from_pips(start_price, -t1 * cfg.spike_tolerance, cfg.pip_size):
                    open_trade = Trade(day, cfg.symbol, "short", ts, price, cfg.lot_size, dn_t1, dn_t2, start_price)
                    continue
            if open_trade and open_hedge is None:
                if open_trade.direction == "long" and price >= open_trade.t2_level:
                    profit = pnl_usd(price - open_trade.entry_price, cfg.pip_size, open_trade.lot, cfg.pip_value_per_lot)
                    trade_logs.append({"day": day, "symbol": cfg.symbol, "type": "solo_exit_T2",
                                       "entry_time": open_trade.entry_time.isoformat(),
                                       "exit_time": ts.isoformat(), "direction": "long",
                                       "entry": open_trade.entry_price, "exit": price,
                                       "lot": open_trade.lot, "profit_usd": profit})
                    open_trade = None
                    continue
                if open_trade.direction == "short" and price <= open_trade.t2_level:
                    profit = pnl_usd(open_trade.entry_price - price, cfg.pip_size, open_trade.lot, cfg.pip_value_per_lot)
                    trade_logs.append({"day": day, "symbol": cfg.symbol, "type": "solo_exit_T2",
                                       "entry_time": open_trade.entry_time.isoformat(),
                                       "exit_time": ts.isoformat(), "direction": "short",
                                       "entry": open_trade.entry_price, "exit": price,
                                       "lot": open_trade.lot, "profit_usd": profit})
                    open_trade = None
                    continue
                retrace = (price <= start_price if open_trade.direction == "long" else price >= start_price)
                if retrace:
                    hedge_dir = "short" if open_trade.direction == "long" else "long"
                    open_hedge = Hedge(ts, price, open_trade.lot * 2.0, hedge_dir)
                    continue
            if open_trade and open_hedge:
                if open_trade.direction == "long":
                    pnl_trade = pnl_usd(price - open_trade.entry_price, cfg.pip_size, open_trade.lot, cfg.pip_value_per_lot)
                else:
                    pnl_trade = pnl_usd(open_trade.entry_price - price, cfg.pip_size, open_trade.lot, cfg.pip_value_per_lot)

                if open_hedge.direction == "long":
                    pnl_hedge = pnl_usd(price - open_hedge.entry_price, cfg.pip_size, open_hedge.lot, cfg.pip_value_per_lot)
                    hedge_move_pips = pips_between(open_hedge.entry_price, price, cfg.pip_size)
                else:
                    pnl_hedge = pnl_usd(open_hedge.entry_price - price, cfg.pip_size, open_hedge.lot, cfg.pip_value_per_lot)
                    hedge_move_pips = pips_between(price, open_hedge.entry_price, cfg.pip_size)

                combined = pnl_trade + pnl_hedge
                min_move_ok = hedge_move_pips >= cfg.hedge_min_move_pips
                if combined >= cfg.hedge_close_spike or (combined >= cfg.hedge_close_pref and min_move_ok):
                    trade_logs.append({"day": day, "symbol": cfg.symbol, "type": "hedge_close",
                                       "entry_time": open_trade.entry_time.isoformat(),
                                       "exit_time": ts.isoformat(), "direction": open_trade.direction,
                                       "entry": open_trade.entry_price, "exit": price,
                                       "lot": open_trade.lot, "profit_usd": pnl_trade})
                    trade_logs.append({"day": day, "symbol": cfg.symbol, "type": "hedge_close",
                                       "entry_time": open_hedge.open_time.isoformat(),
                                       "exit_time": ts.isoformat(), "direction": open_hedge.direction,
                                       "entry": open_hedge.entry_price, "exit": price,
                                       "lot": open_hedge.lot, "profit_usd": pnl_hedge})
                    open_trade = None
                    open_hedge = None
                    continue

        # EOD close MTM
        if open_trade is not None:
            last_price = float(day_df["price"].iloc[-1])
            if open_trade.direction == "long":
                pt = pnl_usd(last_price - open_trade.entry_price, cfg.pip_size, open_trade.lot, cfg.pip_value_per_lot)
            else:
                pt = pnl_usd(open_trade.entry_price - last_price, cfg.pip_size, open_trade.lot, cfg.pip_value_per_lot)
            trade_logs.append({"day": day, "symbol": cfg.symbol, "type": "eod_close",
                               "entry_time": open_trade.entry_time.isoformat(),
                               "exit_time": day_df.index[-1].isoformat(), "direction": open_trade.direction,
                               "entry": open_trade.entry_price, "exit": last_price,
                               "lot": open_trade.lot, "profit_usd": pt})
        if open_hedge is not None:
            last_price = float(day_df["price"].iloc[-1])
            if open_hedge.direction == "long":
                ph = pnl_usd(last_price - open_hedge.entry_price, cfg.pip_size, open_hedge.lot, cfg.pip_value_per_lot)
            else:
                ph = pnl_usd(open_hedge.entry_price - last_price, cfg.pip_size, open_hedge.lot, cfg.pip_value_per_lot)
            trade_logs.append({"day": day, "symbol": cfg.symbol, "type": "eod_close_hedge",
                               "entry_time": open_hedge.open_time.isoformat(),
                               "exit_time": day_df.index[-1].isoformat(), "direction": open_hedge.direction,
                               "entry": open_hedge.entry_price, "exit": last_price,
                               "lot": open_hedge.lot, "profit_usd": ph})
        day_profit = sum(t["profit_usd"] for t in trade_logs)
        summary = {"day": day, "symbol": cfg.symbol, "profit_usd": day_profit, "num_legs": len(trade_logs)}
        return {"day": day, "summary": summary, "trades": trade_logs, "anchor_price": start_price}

def parse_csv(path: str, default_symbol: Optional[str] = None, tz: ZoneInfo = SERVER_TZ) -> pd.DataFrame:
    """Accepts multiple schemas: 
       - timestamp,price,(optional symbol)
       - timestamp,open,high,low,close,(optional symbol) -> uses close
       - timestamp,bid,ask,(optional symbol) -> uses mid=(bid+ask)/2
    """
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    if "timestamp" not in cols:
        raise ValueError(f"{path}: missing 'timestamp' column")
    if "price" in cols:
        price = df[cols["price"]]
    elif {"close","open","high","low"}.issubset(set(k.lower() for k in df.columns)):
        price = df[cols["close"]]
    elif "bid" in cols and "ask" in cols:
        price = (df[cols["bid"]] + df[cols["ask"]]) / 2.0
    else:
        raise ValueError(f"{path}: cannot infer price column (need price or OHLC or bid/ask)")
    symbol = None
    for key in df.columns:
        if key.lower() == "symbol":
            symbol = df[key].astype(str).iloc[0]
            break
    if symbol is None:
        # infer from filename if not provided
        base = os.path.basename(path)
        guess = os.path.splitext(base)[0].upper()
        # common cases: "EURUSD_2025", "prices_XAUUSD", etc.
        for s in ("EURUSD","GBPUSD","USDJPY","XAGUSD","XAUUSD"):
            if s in guess:
                symbol = s
                break
    if symbol is None:
        symbol = default_symbol or "UNKNOWN"
    ts = pd.to_datetime(df[cols["timestamp"]], utc=False, infer_datetime_format=True, errors="coerce")
    if ts.dt.tz is None or str(ts.dt.tz.iloc[0]) == "None":
        ts = ts.dt.tz_localize(tz)
    else:
        ts = ts.dt.tz_convert(tz)
    out = pd.DataFrame({"price": price.values, "symbol": symbol})
    out.index = ts
    out = out.sort_index()
    return out

def resample_to_5m(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("5min").last().dropna()

def split_by_server_day(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    days = {}
    for day, part in df.groupby(df.index.date):
        days[str(day)] = part
    return days

def run_backtest_for_symbol(prices_5m: pd.DataFrame, cfg: SymbolConfig, out_root: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    days = split_by_server_day(prices_5m)
    bt = ThresholdHedgeBacktester(cfg)
    all_trades, all_summaries = [], []
    logs_dir = os.path.join(out_root, cfg.symbol)
    os.makedirs(logs_dir, exist_ok=True)
    for day, day_df in days.items():
        out = bt.run_day(day_df)
        with open(os.path.join(logs_dir, f"{day}.json"), "w") as f:
            json.dump(out, f, indent=2)
        all_trades.extend(out["trades"])
        if "summary" in out and out["summary"]:
            all_summaries.append(out["summary"])
    trades_df = pd.DataFrame(all_trades)
    summaries_df = pd.DataFrame(all_summaries)
    trades_df.to_csv(os.path.join(logs_dir, f"{cfg.symbol}_trades_5m.csv"), index=False)
    summaries_df.to_csv(os.path.join(logs_dir, f"{cfg.symbol}_summary_5m.csv"), index=False)
    return trades_df, summaries_df

DEFAULT_CFGS: Dict[str, SymbolConfig] = {
    "EURUSD": SymbolConfig("EURUSD", threshold_pips=15, pip_size=0.0001, lot_size=0.5, pip_value_per_lot=10.0),
    "GBPUSD": SymbolConfig("GBPUSD", threshold_pips=15, pip_size=0.0001, lot_size=0.5, pip_value_per_lot=10.0),
    "USDJPY": SymbolConfig("USDJPY", threshold_pips=15, pip_size=0.01,   lot_size=0.5, pip_value_per_lot=9.1),
    "XAGUSD": SymbolConfig("XAGUSD", threshold_pips=20, pip_size=0.01,   lot_size=0.5, pip_value_per_lot=5.0),
    "XAUUSD": SymbolConfig("XAUUSD", threshold_pips=20, pip_size=0.1,    lot_size=0.5, pip_value_per_lot=10.0),
}

def discover_csvs(root: str) -> List[str]:
    paths = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower().endswith(".csv"):
                paths.append(os.path.join(dirpath, fn))
    return sorted(paths)

def main():
    # args: input_dir [output_dir] [config_json_path]
    if len(sys.argv) < 2:
        print("Usage: python run_backtest_5m.py <input_dir> [output_dir] [config_json_path]")
        sys.exit(1)
    in_dir = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) >= 3 else os.path.join(in_dir, "_backtest_5m_outputs")
    cfg_path = sys.argv[3] if len(sys.argv) >= 4 else None
    os.makedirs(out_dir, exist_ok=True)

    # Load custom configs if provided
    cfgs = dict(DEFAULT_CFGS)
    if cfg_path and os.path.exists(cfg_path):
        with open(cfg_path, "r") as f:
            raw = json.load(f)
        for sym, c in raw.items():
            cfgs[sym.upper()] = SymbolConfig(
                symbol=sym.upper(),
                threshold_pips=float(c.get("threshold_pips", cfgs.get(sym.upper(), DEFAULT_CFGS.get("EURUSD")).threshold_pips)),
                pip_size=float(c.get("pip_size", cfgs.get(sym.upper(), DEFAULT_CFGS.get("EURUSD")).pip_size)),
                lot_size=float(c.get("lot_size", cfgs.get(sym.upper(), DEFAULT_CFGS.get("EURUSD")).lot_size)),
                pip_value_per_lot=float(c.get("pip_value_per_lot", cfgs.get(sym.upper(), DEFAULT_CFGS.get("EURUSD")).pip_value_per_lot)),
                t2_multiple=float(c.get("t2_multiple", 2.0)),
                spike_tolerance=float(c.get("spike_tolerance", 1.25)),
                hedge_min_move_pips=float(c.get("hedge_min_move_pips", 5.0)),
                hedge_close_pref=float(c.get("hedge_close_pref", 25.0)),
                hedge_close_spike=float(c.get("hedge_close_spike", 50.0)),
            )

    csvs = discover_csvs(in_dir)
    if not csvs:
        print(f"No CSVs found in {in_dir}")
        sys.exit(2)

    combined_trades = []
    combined_summary = []
    for path in csvs:
        df = parse_csv(path)
        sym = str(df["symbol"].iloc[0]).upper()
        if sym not in cfgs:
            print(f"[WARN] No config for {sym}. Skipping {path}. Provide config JSON with pip_size/threshold_pips.")
            continue
        close = df[["price"]]
        prices_5m = close.resample("5min").last().dropna()
        trades_df, summary_df = run_backtest_for_symbol(prices_5m, cfgs[sym], out_root=out_dir)
        trades_df["symbol"] = sym
        summary_df["symbol"] = sym
        combined_trades.append(trades_df)
        combined_summary.append(summary_df)

    if combined_trades:
        all_trades = pd.concat(combined_trades, ignore_index=True)
        all_trades.to_csv(os.path.join(out_dir, "all_trades_5m.csv"), index=False)
    else:
        all_trades = pd.DataFrame()
    if combined_summary:
        all_summary = pd.concat(combined_summary, ignore_index=True)
        all_summary.to_csv(os.path.join(out_dir, "all_summary_5m.csv"), index=False)
    else:
        all_summary = pd.DataFrame()

    # Write a small report
    report = {
        "inputs_dir": in_dir,
        "outputs_dir": out_dir,
        "symbols_processed": sorted(list(set(s for s in (all_summary["symbol"].unique() if len(all_summary) else [])))),
        "total_days": int(all_summary["day"].nunique()) if len(all_summary) else 0,
        "total_legs": int(all_trades.shape[0]) if len(all_trades) else 0,
        "net_pnl_usd": float(all_trades["profit_usd"].sum()) if len(all_trades) else 0.0
    }
    with open(os.path.join(out_dir, "report.json"), "w") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
