"""Cost model: the default-on assumptions every backtest carries.

v0.2.0 made costs **default-on**: a bare ``SimulatedExecutionHandler()``
applies ``CostModel.defaults()`` instead of zero costs. The rationale is
the one this release is named for -- price-return backtests lie quietly,
and the quietest lie is the zero-cost fill. Every ``BacktestResult`` now
carries an ``assumptions`` block (see ``engine``) that states exactly
which cost model ran, so a reader can never mistake a costed run for a
cost-free one.

The model decomposes each fill into three priced legs plus an optional
borrow leg:

* **commission** -- any ``execution.Commission`` schedule (default
  ``PerShareCommission(0.005)``, i.e. half a cent per share);
* **slippage** -- adverse bps, with *separate* entry and exit knobs
  (default 5 bps each way). Entries and exits face different urgency and
  different market impact; one knob for both was false precision;
* **half-spread** -- the cost of crossing half the bid/ask spread, per
  side (default 1 bp). Slippage moves the fill *price*; the half-spread
  is charged to cash separately and recorded on the fill, so the two
  never double-count;
* **borrow** -- annualized bps on short market value, accrued daily
  actual/365 (default 50 bps/yr). Applied by ``Portfolio.accrue_borrow_cost``.

``market_impact`` is a hook for a user-supplied impact function; ``None``
(the default) means market impact is *not modeled*, and the assumptions
block says so out loud.

``CostModel.disabled(reason)`` is the explicit opt-out: every cost leg
goes to zero, and the *reason* is mandatory -- a disabled model without
a stated reason is just the old silent zero, so it is rejected.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from math import isfinite

from .execution import Commission, NoCommission, PerShareCommission


def _non_negative(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative finite number, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class CostModel:
    """All cost assumptions for a backtest, in one declared place."""

    commission: Commission = field(default_factory=lambda: PerShareCommission(0.005))
    slippage_entry_bps: float = 5.0
    slippage_exit_bps: float = 5.0
    half_spread_bps: float = 1.0
    borrow_cost_annual_bps: float = 50.0
    market_impact: Callable[[float, float], float] | None = None
    disabled_reason: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "slippage_entry_bps",
            "slippage_exit_bps",
            "half_spread_bps",
            "borrow_cost_annual_bps",
        ):
            object.__setattr__(self, name, _non_negative(name, getattr(self, name)))
        if self.disabled_reason is not None and not self.disabled_reason.strip():
            raise ValueError("disabled_reason must be non-empty when given")

    @classmethod
    def defaults(cls) -> "CostModel":
        """The default-on model: 5 bps slippage each way, 1 bp half-spread,
        $0.005/share commission, 50 bps/yr borrow. Applied automatically by
        a bare ``SimulatedExecutionHandler()``."""
        return cls()

    @classmethod
    def disabled(cls, reason: str) -> "CostModel":
        """Explicit opt-out: every cost leg is zero. ``reason`` is required
        and must be non-empty -- silent zero-cost runs are not a thing."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("CostModel.disabled() requires a non-empty reason string")
        return cls(
            commission=NoCommission(),
            slippage_entry_bps=0.0,
            slippage_exit_bps=0.0,
            half_spread_bps=0.0,
            borrow_cost_annual_bps=0.0,
            disabled_reason=reason.strip(),
        )

    @property
    def is_disabled(self) -> bool:
        return self.disabled_reason is not None

    def as_dict(self) -> dict:
        """Plain-data view for the ``assumptions`` block."""
        return {
            "commission": _commission_as_dict(self.commission),
            "slippage_entry_bps": self.slippage_entry_bps,
            "slippage_exit_bps": self.slippage_exit_bps,
            "half_spread_bps": self.half_spread_bps,
            "borrow_cost_annual_bps": self.borrow_cost_annual_bps,
            "market_impact": "provided" if self.market_impact is not None else None,
            "disabled": self.is_disabled,
            "disabled_reason": self.disabled_reason,
        }


def _commission_as_dict(commission: Commission) -> dict:
    """Serialize a commission schedule without importing its module's
    internals at runtime -- duck-typed on the known schedule shapes."""
    params: dict[str, float] = {}
    for attr in ("fee", "rate"):
        if hasattr(commission, attr):
            params[attr] = float(getattr(commission, attr))
    return {"type": type(commission).__name__, "params": params}


def fill_cost(price: float, quantity: float, is_entry: bool, model: CostModel) -> dict:
    """Decompose one fill's cost into its priced legs.

    ``price`` is the raw (pre-slippage) fill price; slippage and spread
    are quoted against it so hand-checks stay exact. Returns
    ``{"commission", "slippage", "spread", "total"}`` in dollars.
    """
    bps = model.slippage_entry_bps if is_entry else model.slippage_exit_bps
    slippage = price * quantity * bps / 10_000.0
    spread = price * quantity * model.half_spread_bps / 10_000.0
    commission = model.commission.cost(quantity, price)
    return {
        "commission": commission,
        "slippage": slippage,
        "spread": spread,
        "total": commission + slippage + spread,
    }
