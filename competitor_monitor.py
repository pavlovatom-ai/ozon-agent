from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CompetitorCheck:
    our_price: float
    competitor_price: float
    minimum_safe_price: Optional[float] = None

    @property
    def status(self) -> str:
        if self.minimum_safe_price is not None and self.competitor_price < self.minimum_safe_price:
            return "below_safe_price"
        if self.competitor_price < self.our_price:
            return "undercutting"
        if self.competitor_price > self.our_price:
            return "above_our_price"
        return "same_price"

    @property
    def difference(self) -> float:
        return self.competitor_price - self.our_price


def check_competitor(our_price: float, competitor_price: float, minimum_safe_price: Optional[float] = None) -> CompetitorCheck:
    return CompetitorCheck(
        our_price=max(float(our_price), 0.0),
        competitor_price=max(float(competitor_price), 0.0),
        minimum_safe_price=minimum_safe_price,
    )
