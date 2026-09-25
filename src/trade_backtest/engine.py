"""The event loop that ties data, strategy, portfolio, and execution together.

One pass over the bar stream, six steps per timestamp:

1. Fill orders pending from the previous timestamp at this bar's open
   (no lookahead bias).
2. Apply corporate actions whose ex-date is this bar's date (before
   mark-to-market, so reinvested dividends buy at the ex-date close).
3. Mark the portfolio to market.
4. Accrue borrow cost on short market value (actual/365).
5. Ask the strategy for signals on this bar; convert to orders, held for
   the next bar.
6. Record the equity point.

Results carry the full equity curve, closed trades, headline metrics via
:func:`performance.summarize`, and -- always -- an ``assumptions`` dict.
No report without it: every number in a backtest rests on declared
choices about data basis, corporate actions, and costs, and the report
states them all.
"""

from __future__ import annotations

from dataclasses import dataclass

from .data import BarDataHandler
from .exceptions import BacktestError
from .execution import SimulatedExecutionHandler
from .models import EquityPoint, Order, Signal, Trade
from .performance import summarize
from .portfolio import Portfolio
from .strategy import Strategy
from .total_return import AdjustedDataHandler, AdjustmentBasis, Dividend


@dataclass
class BacktestResult:
    symbols: list[str]
    initial_cash: float
    final_equity: float
    equity_curve: list[EquityPoint]
    trades: list[Trade]
    metrics: dict[str, float]
    assumptions: dict  # required: no report without it
    periods_per_year: float = 252

    @property
    def num_trades(self) -> int:
        return len(self.trades)


#: Sentinel: ``adjustment_basis`` has no usable default, but a missing
#: value must raise ``BacktestError`` (not ``TypeError``), loudly, at
#: construction.
_REQUIRED = object()


class BacktestEngine:
    """Event-driven backtester over a bar stream.

    ``adjustment_basis`` is **required** (keyword-only): declare
    ``"pre_adjusted"``, ``"unadjusted_with_events"``, or ``"none"``.
    Missing, ``None``, or unknown values raise ``BacktestError`` at
    construction -- the basis of the bars is never silent. When
    ``corporate_actions`` are given, the basis must be
    ``"unadjusted_with_events"`` and the data is wrapped in
    ``AdjustedDataHandler`` (splits backward-adjusted; dividends routed
    through cash).
    """

    def __init__(
        self,
        data: BarDataHandler,
        strategy: Strategy,
        portfolio: Portfolio,
        execution: SimulatedExecutionHandler | None = None,
        periods_per_year: float = 252,
        risk_free: float = 0.0,
        *,
        adjustment_basis: str = _REQUIRED,  # type: ignore[assignment]
        adjustment_note: str = "",
        corporate_actions: list = (),
    ) -> None:
        if adjustment_basis is _REQUIRED:
            raise BacktestError(
                "adjustment_basis is required: declare 'pre_adjusted', "
                "'unadjusted_with_events', or 'none'. There is no silent default."
            )
        self.adjustment_basis = AdjustmentBasis.validate(adjustment_basis)
        self.adjustment_note = adjustment_note
        self.corporate_actions = list(corporate_actions)
        if self.corporate_actions:
            if self.adjustment_basis != AdjustmentBasis.UNADJUSTED_WITH_EVENTS:
                raise BacktestError(
                    "corporate_actions require "
                    "adjustment_basis='unadjusted_with_events'; got "
                    f"{self.adjustment_basis!r}"
                )
            data = AdjustedDataHandler(data, self.corporate_actions)
        self.data = data
        self.strategy = strategy
        self.portfolio = portfolio
        self.execution = execution or SimulatedExecutionHandler()
        self.periods_per_year = periods_per_year
        self.risk_free = risk_free

    def run(self) -> BacktestResult:
        pending: list[Order] = []
        prev_ts = None
        dividends_applied = 0
        splits_applied = 0
        for timestamp, bars in self.data.stream():
            # 1. Fill orders signaled on the previous bar, at this bar's open.
            for fill in self.execution.fill(pending, bars):
                self.portfolio.on_fill(fill)
            pending = []
            # 2. Corporate actions with ex-date == bar date, before MTM.
            #    Actions whose ex-date never appears in the bar stream are
            #    skipped (and not counted).
            for action in self.corporate_actions:
                if action.ex_date == timestamp.date():
                    self.portfolio.apply_corporate_action(
                        action, bars.get(action.symbol)
                    )
                    if isinstance(action, Dividend):
                        dividends_applied += 1
                    else:
                        splits_applied += 1
            # 3-4. Revalue, then accrue borrow on shorts (actual/365).
            self.portfolio.mark_to_market(timestamp, bars)
            self.portfolio.accrue_borrow_cost(timestamp, prev_ts)
            prev_ts = timestamp
            # 5. Strategy decides; signals -> orders, held for next bar.
            signals: list[Signal] = self.strategy.on_bar(timestamp, bars)
            for signal in signals:
                order = self.portfolio.on_signal(signal, bars)
                if order is not None:
                    pending.append(order)
            # 6. Record.
            self.portfolio.record(timestamp)

        curve = self.portfolio.equity_curve
        metrics = summarize(curve, self.portfolio.trades, self.periods_per_year, self.risk_free)
        assumptions = self._assumptions(dividends_applied, splits_applied)
        return BacktestResult(
            symbols=self.data.symbols,
            initial_cash=self.portfolio.initial_cash,
            final_equity=self.portfolio.equity,
            equity_curve=curve,
            trades=list(self.portfolio.trades),
            metrics=metrics,
            assumptions=assumptions,
            periods_per_year=self.periods_per_year,
        )

    def _assumptions(self, dividends_applied: int, splits_applied: int) -> dict:
        # Deferred: the package __init__ imports this module.
        from . import __version__ as engine_version

        model = self.portfolio.cost_model
        market_impact_note = (
            "market impact (no market_impact hook provided)"
            if model.market_impact is None
            else "market impact (market_impact hook provided but not applied to fills)"
        )
        return {
            "engine_version": engine_version,
            "adjustment_basis": self.adjustment_basis,
            "adjustment_note": self.adjustment_note,
            "corporate_actions_applied": {
                "dividends": dividends_applied,
                "splits": splits_applied,
            },
            "dividend_reinvestment": self.portfolio.dividend_reinvestment,
            "cost_model": model.as_dict(),
            "borrow": {
                "annual_bps": model.borrow_cost_annual_bps,
                "total_paid": self.portfolio.total_borrow_cost,
                "day_count": "actual/365",
            },
            "not_modeled": [
                market_impact_note,
                "partial fills",
                "latency",
                "margin calls",
            ],
            "periods_per_year": self.periods_per_year,
        }
