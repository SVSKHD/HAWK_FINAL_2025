from __future__ import annotations
from config import SYMBOL_CONFIGS
from datetime import datetime, timezone

ENTRY_MIN = 1.00
ENTRY_MAX = 1.25
CLOSE_AT  = 2.00


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Threshold:
    def __init__(self, symbol: str, start: float, current: float, high: float, low: float):
        self.symbol = symbol
        self.start = float(start)
        self.current = float(current)
        self.high = float(high)
        self.low = float(low)

        self.symbol_data = SYMBOL_CONFIGS.get(symbol)
        if not self.symbol_data:
            raise ValueError(f"Symbol config not found for: {symbol}")

        self.pip_size = float(self.symbol_data.pip_size)
        self.threshold_pips = int(self.symbol_data.threshold_pips)

        self.result = {
            "symbol": self.symbol,
            "start": self.start,
            "current": self.current,
            "high": self.high,
            "low": self.low,
            "pip_size": self.pip_size,
            "config_threshold_pips": self.threshold_pips,
            "pip_difference_pips": None,
            "abs_pip_difference_pips": None,
            "threshold_scale": None,
            "abs_threshold_scale": None,
            "executables": None,
        }

    def calculate_pip_difference(self) -> float:
        raw_diff = self.current - self.start
        pip_diff = round(raw_diff / self.pip_size, 2)
        self.result["pip_difference_pips"] = pip_diff
        self.result["abs_pip_difference_pips"] = round(abs(pip_diff), 2)
        return pip_diff

    def calculate_threshold_scale(self) -> float:
        pip_diff = self.result.get("pip_difference_pips") or self.calculate_pip_difference()
        scale = round(pip_diff / float(self.threshold_pips), 4)
        self.result["threshold_scale"] = scale
        self.result["abs_threshold_scale"] = round(abs(scale), 4)
        return scale

    def compute_direction(self) -> str:
        if self.current > self.start:
            return "buy"
        elif self.current < self.start:
            return "sell"
        return "neutral"

    def decide_action(self) -> dict:
        scale = self.calculate_threshold_scale()
        abs_scale = abs(scale)
        direction = self.compute_direction()

        is_above_threshold = abs_scale >= 1.0
        in_entry_window = ENTRY_MIN <= abs_scale <= ENTRY_MAX
        should_close = abs_scale >= CLOSE_AT

        if should_close:
            action = "close"
        elif in_entry_window:
            action = "place_long_trade" if scale > 0 else "place_short_trade"
        else:
            action = "wait"

        return {
            "direction": direction,
            "is_above_threshold": is_above_threshold,
            "in_entry_window": in_entry_window,
            "should_close": should_close,
            "action": action,
            "first_threshold_reached_at": None,
            "second_threshold_reached_at": None,
        }

    def check_latest_high_latest_low(self, executables: dict) -> dict:
        breach_high = self.current > self.high
        breach_low = self.current < self.low
        dist_above_high_pips = round(max(0.0, (self.current - self.high) / self.pip_size), 2)
        dist_below_low_pips = round(max(0.0, (self.low - self.current) / self.pip_size), 2)

        executables.update({
            "breach_high": breach_high,
            "breach_low": breach_low,
            "dist_above_high_pips": dist_above_high_pips,
            "dist_below_low_pips": dist_below_low_pips,
            "strong_buy": breach_high,
            "strong_sell": breach_low,
        })
        return executables

    def stamp_threshold_times(self, executables: dict, previous_executables: dict | None) -> dict:
        if previous_executables:
            executables["first_threshold_reached_at"] = previous_executables.get("first_threshold_reached_at")
            executables["second_threshold_reached_at"] = previous_executables.get("second_threshold_reached_at")

        abs_scale = self.result["abs_threshold_scale"]

        if abs_scale >= 1.0 and not executables.get("first_threshold_reached_at"):
            executables["first_threshold_reached_at"] = _now_iso_utc()
        if abs_scale >= 2.0 and not executables.get("second_threshold_reached_at"):
            executables["second_threshold_reached_at"] = _now_iso_utc()

        return executables

    def run(self, previous_executables: dict | None = None) -> dict:
        self.calculate_pip_difference()
        self.calculate_threshold_scale()
        execs = self.decide_action()
        execs = self.check_latest_high_latest_low(execs)
        execs = self.stamp_threshold_times(execs, previous_executables)
        self.result["executables"] = execs
        return self.result


# ---- Example: line-by-line output ----
if __name__ == "__main__":
    t1 = Threshold("XAUUSD", start=3999.00, current=4006.00, high=4009.77, low=4000.76)
    result = t1.run()

    print("=== Threshold Result (Line by Line) ===")
    for key, value in result.items():
        if key != "executables":
            print(f"{key}: {value}")
    print("\n--- Executables ---")
    for key, value in result["executables"].items():
        print(f"{key}: {value}")
