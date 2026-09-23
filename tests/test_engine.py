"""End-to-end engine test on a hand-computed 10-bar scenario.

Closes: 100,101,102,103,104,103,102,101,100,101 (open=high=low=close).
SMA(2)/SMA(3) long-only strategy, fixed 10 units, no costs:

* Day 3: fast crosses above slow -> LONG, filled Day 4 open @103.
* Day 7: fast crosses below slow -> EXIT, filled Day 8 open @101.

Expected: one round trip, entry 103, exit 101, pnl -20,
final equity 100000 - 1030 + 1010 = 99980. Fills land on the bar
*after* the signal (no lookahead).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from math import isclose

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


class LongOnlyCrossover(Strategy):
    def __init__(self) -> None:
        super().__init__(["TEST"])
        self._closes: deque[float] = deque(maxlen=3)

    def _sma(self, n: int):
        return sum(list(self._closes)[-n:]) / n if len(self._closes) >= n else None

    def on_bar(self, timestamp, bars):
        close = bars["TEST"].close
        pf, ps = self._sma(2), self._sma(3)
        self._closes.append(close)
        f, s = self._sma(2), self._sma(3)
        if f is None or s is None:
            return []
        if f > s and (pf is None or ps is None or pf <= ps):
            return [Signal("TEST", timestamp, SignalAction.LONG)]
        if f < s and (pf is None or ps is None or pf >= ps):
            return [Signal("TEST", timestamp, SignalAction.EXIT)]
        return []


def make_bars():
    closes = [100, 101, 102, 103, 104, 103, 102, 101, 100, 101]
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return [
        normalize_bar({
            "symbol": "TEST",
            "timestamp": start + timedelta(days=i),
            "open": c, "high": c, "low": c, "close": c, "volume": 1000,
        })
        for i, c in enumerate(closes)
    ]


def run_engine(**kwargs):
    execution = kwargs.pop("execution", SimulatedExecutionHandler(commission=NoCommission()))
    engine = BacktestEngine(
        data=ListDataHandler(make_bars()),
        strategy=LongOnlyCrossover(),
        portfolio=Portfolio(100_000.0, FixedQuantitySizer(10)),
        execution=execution,
    )
    return engine.run()


def test_one_round_trip_with_expected_prices():
    result = run_engine()
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == 103.0  # day-4 open, the bar AFTER the signal
    assert trade.exit_price == 101.0   # day-8 open, the bar AFTER the exit signal
    assert trade.quantity == 10
    assert trade.pnl == -20.0


def test_final_equity_matches_hand_computation():
    result = run_engine()
    assert isclose(result.final_equity, 99_980.0, rel_tol=1e-9)
    assert isclose(result.metrics["total_return"], -0.0002, rel_tol=1e-9)


def test_equity_curve_covers_every_bar():
    result = run_engine()
    assert len(result.equity_curve) == 10
    assert isclose(result.equity_curve[-1].equity, 99_980.0, rel_tol=1e-9)


def test_flat_after_exit():
    result = run_engine()
    assert result.equity_curve[-1].cash == result.equity_curve[-1].equity


def test_slippage_and_commission_change_outcome():
    from trade_backtest import PerShareCommission
    result = run_engine(
        execution=SimulatedExecutionHandler(
            slippage_bps=100.0,  # 1% adverse
            commission=PerShareCommission(0.01),
        )
    )
    trade = result.trades[0]
    # Buy lifted 1%: 103 -> 104.03 ; sell hit 1%: 101 -> 99.99
    assert isclose(trade.entry_price, 104.03, rel_tol=1e-9)
    assert isclose(trade.exit_price, 99.99, rel_tol=1e-9)
    assert trade.pnl < -20.0  # costs make it worse
    assert result.final_equity < 99_980.0
