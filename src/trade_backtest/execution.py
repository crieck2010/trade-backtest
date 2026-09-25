"""Simulated execution: commissions, slippage, and fill logic.

Orders signaled on bar *t* fill at bar *t+1*'s open -- the engine holds
them as pending for one step, so strategies can never trade on the same
bar's close they just observed (no lookahead bias). Limit orders fill
when the bar's range touches the limit price.

Costs (v0.2.0: default-on):

* **Slippage** in basis points, applied adversely (buys lift, sells hit),
  with *separate* entry and exit knobs. The portfolio marks each order
  ``is_entry`` (dominant-leg rule for reversals); execution picks the
  knob from it.
* **Half-spread**: the cost of crossing half the spread, per side,
  recorded on the fill and charged to cash (not baked into the price).
* **Commission** models: flat per trade, per share/unit, or percent of
  notional. Futures/options overrides can key off ``ContractSpec`` later.

Resolution rule for the constructor (documented, tested):

1. ``cost_model`` given -> use it; the new keyword knobs
   (``slippage_entry_bps=`` etc.) override its fields via
   ``dataclasses.replace``.
2. *Nothing* explicitly passed -> ``CostModel.defaults()`` (DEFAULT-ON;
   this is the v0.2.0 behavior change -- a bare handler is no longer
   cost-free).
3. Old-style ``slippage_bps`` and/or ``commission`` passed ->
   LEGACY mode: start from a zeroed model and apply only what was
   passed (explicit ``slippage_bps`` sets both knobs). This keeps
   callers like ``trade-swing`` -- which applies its own post-hoc cost
   model -- at exactly zero cost.
4. New knobs without ``cost_model`` -> ``CostModel.defaults()`` with
   those fields overridden.

The cost decomposition of every fill is available as
``Fill.total_cost`` (commission + slippage + spread).
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from math import isfinite

from .models import Bar, Fill, Order, OrderAction, OrderType

#: Sentinel distinguishing "caller passed slippage_bps" from the default.
_UNSET = object()


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

    :param slippage_bps: legacy adverse price move per fill, in basis
        points. Passing it (at all) selects LEGACY mode; it sets both
        the entry and exit knobs.
    :param commission: legacy commission schedule. Passing it (anything
        but ``None``) selects LEGACY mode.
    :param cost_model: a ``costs.CostModel``; when given, it wins and the
        new keyword knobs override its fields.
    :param slippage_entry_bps: with ``cost_model`` (or alone, on top of
        defaults): adverse bps for orders that open/increase a position.
    :param slippage_exit_bps: adverse bps for orders that close/reduce a
        position.
    :param half_spread_bps: cost of crossing half the spread, per side.

    ``slippage_bps`` (the attribute) is kept for introspection and
    reports the entry knob; new code should read ``cost_model``.
    """

    def __init__(
        self,
        slippage_bps: float = _UNSET,  # type: ignore[assignment]
        commission: Commission | None = None,
        *,
        cost_model=None,
        slippage_entry_bps: float | None = None,
        slippage_exit_bps: float | None = None,
        half_spread_bps: float | None = None,
    ) -> None:
        # Deferred: costs.py imports this module at top level.
        from .costs import CostModel, fill_cost

        self._fill_cost = fill_cost
        overrides = {
            name: value
            for name, value in (
                ("slippage_entry_bps", slippage_entry_bps),
                ("slippage_exit_bps", slippage_exit_bps),
                ("half_spread_bps", half_spread_bps),
            )
            if value is not None
        }
        if cost_model is not None:
            self.cost_model: CostModel = (
                dataclasses.replace(cost_model, **overrides) if overrides else cost_model
            )
            self._legacy = False
        elif overrides:
            self.cost_model = dataclasses.replace(CostModel.defaults(), **overrides)
            self._legacy = False
        elif slippage_bps is _UNSET and commission is None:
            # DEFAULT-ON: a bare handler applies the default cost model.
            self.cost_model = CostModel.defaults()
            self._legacy = False
        else:
            # LEGACY: explicit old-style args -> zero model + what was passed.
            legacy_kwargs: dict = {}
            if slippage_bps is not _UNSET:
                if not isinstance(slippage_bps, (int, float)) or not isfinite(slippage_bps):
                    raise ValueError(
                        f"slippage_bps must be a finite number, got {slippage_bps!r}"
                    )
                if slippage_bps < 0:
                    raise ValueError(
                        f"slippage_bps must be non-negative, got {slippage_bps!r}"
                    )
                legacy_kwargs["slippage_entry_bps"] = float(slippage_bps)
                legacy_kwargs["slippage_exit_bps"] = float(slippage_bps)
            if commission is not None:
                legacy_kwargs["commission"] = commission
            legacy = CostModel.disabled(
                "legacy explicit-cost path: slippage_bps and/or commission were "
                "passed directly; every other cost-model field is zero"
            )
            self.cost_model = (
                dataclasses.replace(legacy, **legacy_kwargs)
                if legacy_kwargs
                else legacy
            )
            self._legacy = True

        # Backward-compatible introspection attributes.
        self.slippage_bps = self.cost_model.slippage_entry_bps
        self.commission = self.cost_model.commission

    @property
    def is_legacy(self) -> bool:
        """True when the handler was built from old-style explicit args."""
        return self._legacy

    def fill(self, orders: list[Order], bars: dict[str, Bar]) -> list[Fill]:
        """Fill ``orders`` against ``bars`` (the bar *after* the signal)."""
        fills: list[Fill] = []
        for order in orders:
            bar = bars.get(order.symbol)
            if bar is None:
                continue  # no bar for this symbol at this timestamp; order lapses
            raw_and_price = self._raw_and_price(order, bar)
            if raw_and_price is None:
                continue  # limit not touched
            raw, price = raw_and_price
            costs = self._fill_cost(raw, order.quantity, order.is_entry, self.cost_model)
            fills.append(
                Fill(
                    symbol=order.symbol,
                    timestamp=bar.timestamp,
                    action=order.action,
                    quantity=order.quantity,
                    price=price,
                    commission=costs["commission"],
                    slippage=costs["slippage"],
                    spread_cost=costs["spread"],
                )
            )
        return fills

    def _raw_and_price(self, order: Order, bar: Bar) -> tuple[float, float] | None:
        """The pre-slippage price and the adverse fill price, or None if a
        limit was not touched."""
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
        bps = (
            self.cost_model.slippage_entry_bps
            if order.is_entry
            else self.cost_model.slippage_exit_bps
        )
        slip = raw * bps / 10_000.0
        price = raw + slip if order.action is OrderAction.BUY else raw - slip
        return raw, price
