"""Simulated execution: commissions, slippage, and fill logic.

Orders signaled on bar *t* fill at bar *t+1*'s open -- the engine holds
them as pending for one step, so strategies can never trade on the same
bar's close they just observed (no lookahead bias). Limit orders fill
when the bar's range touches the limit price.

Costs:

* **Slippage** in basis points, applied adversely (buys lift, sells hit).
* **Commission** models: flat per trade, per share/unit, or percent of
  notional. Futures/options overrides can key off ``ContractSpec`` later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from math import isfinite

from .models import Bar, Fill, Order, OrderAction, OrderType


class Commission(ABC):
    """Commission schedule."""

    @abstractmethod
    def cost(self, quantity: float, price: float) -> float:
        raise NotImplementedError


class FlatCommission(Commission):
    """Fixed fee per fill (e.g. $1 per trade)."""

    def __init__(self, fee: float) -> None:
        if fee < 0 or not isfinite(fee):
            raise ValueError(f"fee must be non-negative, got {fee!r}")
        self.fee = float(fee)

    def cost(self, quantity: float, price: float) -> float:
        return self.fee


class PerShareCommission(Commission):
    """Per-unit fee (e.g. $0.005/share)."""

    def __init__(self, rate: float) -> None:
        if rate < 0 or not isfinite(rate):
            raise ValueError(f"rate must be non-negative, got {rate!r}")
        self.rate = float(rate)

    def cost(self, quantity: float, price: float) -> float:
        return self.rate * quantity


class PercentCommission(Commission):
    """Percent of notional (e.g. 0.0005 = 5 bps)."""

    def __init__(self, rate: float) -> None:
        if rate < 0 or not isfinite(rate):
            raise ValueError(f"rate must be non-negative, got {rate!r}")
        self.rate = float(rate)

    def cost(self, quantity: float, price: float) -> float:
        return self.rate * quantity * price


class NoCommission(Commission):
    def cost(self, quantity: float, price: float) -> float:  # pragma: no cover - trivial
        return 0.0


class SimulatedExecutionHandler:
    """Fill pending orders against the next bar.

    :param slippage_bps: adverse price move per fill, in basis points.
    :param commission: commission schedule applied to every fill.
    """

    def __init__(
        self,
        slippage_bps: float = 0.0,
        commission: Commission | None = None,
    ) -> None:
        if slippage_bps < 0 or not isfinite(slippage_bps):
            raise ValueError(f"slippage_bps must be non-negative, got {slippage_bps!r}")
        self.slippage_bps = float(slippage_bps)
        self.commission = commission if commission is not None else NoCommission()

    def fill(self, orders: list[Order], bars: dict[str, Bar]) -> list[Fill]:
        """Fill ``orders`` against ``bars`` (the bar *after* the signal)."""
        fills: list[Fill] = []
        for order in orders:
            bar = bars.get(order.symbol)
            if bar is None:
                continue  # no bar for this symbol at this timestamp; order lapses
            price = self._fill_price(order, bar)
            if price is None:
                continue  # limit not touched
            fills.append(
                Fill(
                    symbol=order.symbol,
                    timestamp=bar.timestamp,
                    action=order.action,
                    quantity=order.quantity,
                    price=price,
                    commission=self.commission.cost(order.quantity, price),
                )
            )
        return fills

    def _fill_price(self, order: Order, bar: Bar) -> float | None:
        if order.order_type is OrderType.MARKET:
            raw = bar.open
        else:
            limit = order.limit_price
            assert limit is not None
            if order.action is OrderAction.BUY:
                if bar.low > limit:
                    return None
                raw = min(limit, bar.open)
            else:
                if bar.high < limit:
                    return None
                raw = max(limit, bar.open)
        slip = raw * self.slippage_bps / 10_000.0
        return raw + slip if order.action is OrderAction.BUY else raw - slip
