"""The event loop that ties data, strategy, portfolio, and execution together.

One pass over the bar stream:

1. Fill orders pending from the previous timestamp at this bar's open
   (no lookahead bias).
2. Mark the portfolio to market.
3. Ask the strategy for signals on this bar.
4. Convert signals to orders, held for the next bar.
5. Record the equity point.

Results carry the full equity curve, closed trades, and headline
metrics via :func:`performance.summarize`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .data import BarDataHandler
from .execution import SimulatedExecutionHandler
from .models import EquityPoint, Order, Signal, Trade
from .performance import summarize
from .portfolio import Portfolio
from .strategy import Strategy


@dataclass
class BacktestResult:
    symbols: list[str]
    initial_cash: float
    final_equity: float
    equity_curve: list[EquityPoint]
    trades: list[Trade]
    metrics: dict[str, float]
    periods_per_year: float = 252

    @property
    def num_trades(self) -> int:
        return len(self.trades)


class BacktestEngine:
    """Event-driven backtester over a bar stream."""

    def __init__(
        self,
        data: BarDataHandler,
        strategy: Strategy,
        portfolio: Portfolio,
        execution: SimulatedExecutionHandler | None = None,
        periods_per_year: float = 252,
        risk_free: float = 0.0,
    ) -> None:
        self.data = data
        self.strategy = strategy
        self.portfolio = portfolio
        self.execution = execution or SimulatedExecutionHandler()
        self.periods_per_year = periods_per_year
        self.risk_free = risk_free

    def run(self) -> BacktestResult:
        pending: list[Order] = []
        for timestamp, bars in self.data.stream():
            # 1. Fill orders signaled on the previous bar, at this bar's open.
            for fill in self.execution.fill(pending, bars):
                self.portfolio.on_fill(fill)
            pending = []
            # 2-3. Revalue, then let the strategy decide.
            self.portfolio.mark_to_market(timestamp, bars)
            signals: list[Signal] = self.strategy.on_bar(timestamp, bars)
            # 4. Signals -> orders, held for the next bar's open.
            for signal in signals:
                order = self.portfolio.on_signal(signal, bars)
                if order is not None:
                    pending.append(order)
            # 5. Record.
            self.portfolio.record(timestamp)

        curve = self.portfolio.equity_curve
        metrics = summarize(curve, self.portfolio.trades, self.periods_per_year, self.risk_free)
        return BacktestResult(
            symbols=self.data.symbols,
            initial_cash=self.portfolio.initial_cash,
            final_equity=self.portfolio.equity,
            equity_curve=curve,
            trades=list(self.portfolio.trades),
            metrics=metrics,
            periods_per_year=self.periods_per_year,
        )
