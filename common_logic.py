# apps/bot-runner/src/common/price_component.py
from dataclasses import dataclass
from typing import Literal, Optional
from config import SYMBOL_CONFIGS
from notify import send_discord_message  # <-- use your notifier

Direction = Literal["UP", "DOWN", "FLAT"]


@dataclass
class PriceComponent:
    symbol: str
    start_price: float
    current_price: float
    latest_high: float
    latest_low: float

    def __post_init__(self):
        cfg = SYMBOL_CONFIGS.get(self.symbol)
        if not cfg:
            raise ValueError(f"Symbol '{self.symbol}' not found in SYMBOL_CONFIGS.")
        self.pip_size = cfg.pip_size
        self.threshold_pips = cfg.threshold_pips

    @property
    def pips_moved(self) -> float:
        return abs(self.current_price - self.start_price) / self.pip_size

    @property
    def direction(self) -> Direction:
        if self.current_price > self.start_price:
            return "UP"
        elif self.current_price < self.start_price:
            return "DOWN"
        return "FLAT"

    @property
    def strong_direction(self) -> Direction:
        up_pips = (self.latest_high - self.start_price) / self.pip_size
        down_pips = (self.start_price - self.latest_low) / self.pip_size
        if up_pips > down_pips and up_pips >= self.threshold_pips:
            return "UP"
        elif down_pips > up_pips and down_pips >= self.threshold_pips:
            return "DOWN"
        return "FLAT"

    @property
    def immediate(self) -> dict:
        high_pips = (self.latest_high - self.start_price) / self.pip_size
        low_pips = (self.start_price - self.latest_low) / self.pip_size
        direction = "UP" if high_pips > low_pips else "DOWN" if low_pips > high_pips else "FLAT"
        return {
            "latest_high_pips": round(high_pips, 1),
            "latest_low_pips": round(low_pips, 1),
            "direction": direction,
        }

    @property
    def threshold_ratio(self) -> float:
        """1.0 == first threshold; 2.0 == second, etc."""
        return self.pips_moved / self.threshold_pips

    # ---------- NEW: stage logic + in-class notify (info channel) ----------

    def threshold_stage(self) -> int:
        """
        Integer stage of progress:
          0  -> < 1× threshold
          1  -> ≥ 1× threshold
          2  -> ≥ 2× thresholds
          3+ -> further multiples
        """
        r = self.threshold_ratio
        return int(r) if r >= 0 else 0

    def _threshold_message(self, stage: int) -> str:
        d = self.as_dict()
        return (
            f"[{self.symbol}] threshold x{stage} hit | "
            f"start={d['start_price']} cur={d['current_price']} "
            f"pips={d['pips_moved']} dir={d['direction']} "
            f"strong={d['strong_direction']} ratio={d['threshold_ratio']}"
        )

    def notify_threshold_if_hit(self, last_stage_sent: int, *, min_stage: int = 1) -> int:
        """
        Sends to Discord 'info' channel exactly once when crossing new stages.
        Returns the updated last_stage_sent.
        """
        stage_now = self.threshold_stage()
        if stage_now >= min_stage and stage_now > last_stage_sent:
            # Use your notify to the 'info' channel (per your ask)
            send_discord_message("info", self._threshold_message(stage_now))
            return stage_now
        return last_stage_sent

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "start_price": self.start_price,
            "current_price": self.current_price,
            "latest_high": self.latest_high,
            "latest_low": self.latest_low,
            "pips_moved": round(self.pips_moved, 1),
            "direction": self.direction,
            "strong_direction": self.strong_direction,
            "immediate": self.immediate,
            "threshold_ratio": round(self.threshold_ratio, 2),
        }


# if __name__ == "__main__":
#     # quick test example
#     pc = PriceComponent(
#         symbol="EURUSD",
#         start_price=1.1000,
#         current_price=1.1025,
#         latest_high=1.1045,
#         latest_low=1.0990,
#     )
#     print(pc.as_dict())
