"""SMA-crossover demo: long-only strategy on synthetic bars.

Run with ``python examples/sma_crossover.py``. Shows the full loop:
normalize bars -> data handler -> strategy -> engine -> metrics.
"""

from __future__ import annotations

import sys
from collections import deque
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "src")

from trade_backtest import (
    BacktestEngine,
    FixedQuantitySizer,
    ListDataHandler,
    NoCommission,
    Portfolio,
    Signal,
    SignalAction,
    SimulatedExecutionHandler,
    Strategy,
    normalize_bar,
)


class SMACrossover(Strategy):
    """Go long when the fast SMA crosses above the slow SMA; exit on cross down."""

    def __init__(self, symbol: str, fast: int = 5, slow: int = 20) -> None:
        super().__init__([symbol])
        self.fast = fast
        self.slow = slow
        self._closes: deque[float] = deque(maxlen=slow)

    def _sma(self, n: int) -> float | None:
        if len(self._closes) < n:
            return None
        return sum(list(self._closes)[-n:]) / n

    def on_bar(self, timestamp, bars) -> list[Signal]:
        close = bars[self.symbols[0]].close
        prev_fast, prev_slow = self._sma(self.fast), self._sma(self.slow)
        self._closes.append(close)
        fast, slow = self._sma(self.fast), self._sma(self.slow)
        if fast is None or slow is None:
            return []
        crossed_up = fast > slow and (prev_fast is None or prev_slow is None or prev_fast <= prev_slow)
        crossed_down = fast < slow and (prev_fast is None or prev_slow is None or prev_fast >= prev_slow)
        if crossed_up:
            return [Signal(self.symbols[0], timestamp, SignalAction.LONG)]
        if crossed_down:
            return [Signal(self.symbols[0], timestamp, SignalAction.EXIT)]
        return []


def synthetic_bars(symbol: str, n: int = 120) -> list[dict]:
    """A trending-then-choppy series that triggers a few crossovers."""
    import math
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        price = 100 + 0.6 * i + 8 * math.sin(i / 6.0)
        out.append(
            {
                "symbol": symbol,
                "timestamp": start + timedelta(days=i),
                "open": price, "high": price + 0.5,
                "low": price - 0.5, "close": price, "volume": 1_000_000,
            }
        )
    return out


def main() -> None:
    bars = [normalize_bar(b) for b in synthetic_bars("DEMO")]
    engine = BacktestEngine(
        data=ListDataHandler(bars),
        strategy=SMACrossover("DEMO"),
        portfolio=Portfolio(100_000.0, FixedQuantitySizer(100)),
        execution=SimulatedExecutionHandler(
            slippage_bps=1.0, commission=NoCommission()
        ),
        # Synthetic demo bars: no corporate actions exist to adjust for.
        # (Explicit old-style costs above select the legacy zero-spread path;
        # a bare SimulatedExecutionHandler() would apply CostModel.defaults().)
        adjustment_basis="none",
        adjustment_note="synthetic demo bars carry no corporate actions",
    )
    result = engine.run()
    print(f"Symbols:      {result.symbols}")
    print(f"Initial cash: ${result.initial_cash:,.2f}")
    print(f"Final equity: ${result.final_equity:,.2f}")
    print(f"Trades:       {result.num_trades}")
    for name, value in result.metrics.items():
        if name in ("num_trades", "max_drawdown_duration_bars"):
            print(f"{name:28s} {value:.0f}")
        else:
            print(f"{name:28s} {value:+.4f}")
    print(f"Basis:        {result.assumptions['adjustment_basis']}")
    print(f"Costs:        {result.assumptions['cost_model']['commission']}")


if __name__ == "__main__":
    main()
