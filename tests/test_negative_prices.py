"""Regression: negative (and zero) prints must not crash the cost path.

WTI front-month printed -$37.63 on 2020-04-20. Negative prices are real
market data, not corrupt input. Before the fix, ``fill_cost`` computed
``price * quantity * bps`` against *signed* price, producing negative
dollar slippage/spread, and ``Fill.__post_init__`` raised
``ValueError: slippage must be non-negative`` -- the round-3
``CL=F macd_trend`` crash.

The fix quotes every cost leg against notional ``|price| * quantity``
and moves the fill price adversely by sign of *action* (BUY pays more,
SELL receives less), never by sign of price. These tests pin that
behavior through the real, unmocked engine.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from trade_backtest.costs import CostModel, fill_cost
from trade_backtest.data import ListDataHandler
from trade_backtest.engine import BacktestEngine
from trade_backtest.execution import PercentCommission, SimulatedExecutionHandler
from trade_backtest.models import (
    Bar,
    Order,
    OrderAction,
    OrderType,
    Signal,
    SignalAction,
)
from trade_backtest.portfolio import FixedQuantitySizer, Portfolio
from trade_backtest.strategy import Strategy

BASE = datetime(2020, 4, 15, tzinfo=timezone.utc)
# April-2020-shaped: positive -> negative -> positive.
CLOSES = [20.0, 18.5, 10.0, -5.0, -37.63, -10.0, 5.0, 12.0]


def _bars(closes=CLOSES) -> list[Bar]:
    return [
        Bar(
            symbol="CL=F",
            timestamp=BASE + timedelta(days=i),
            open=c,
            high=c + 1.0,
            low=c - 1.0,
            close=c,
            volume=1000,
        )
        for i, c in enumerate(closes)
    ]


def _order(action: OrderAction, is_entry: bool) -> Order:
    return Order(
        symbol="CL=F",
        timestamp=BASE,
        action=action,
        quantity=1.0,
        order_type=OrderType.MARKET,
        is_entry=is_entry,
    )


class TestFillCostSign:
    def test_negative_price_legs_non_negative(self):
        costs = fill_cost(-37.63, 1.0, True, CostModel.defaults())
        assert costs["slippage"] == abs(-37.63) * 1.0 * 5.0 / 10_000.0
        assert costs["spread"] == abs(-37.63) * 1.0 * 1.0 / 10_000.0
        assert costs["slippage"] > 0 and costs["spread"] > 0
        assert costs["total"] == costs["commission"] + costs["slippage"] + costs["spread"]

    def test_zero_price_legs_are_zero_not_nan(self):
        costs = fill_cost(0.0, 1.0, True, CostModel.defaults())
        assert costs["slippage"] == 0.0
        assert costs["spread"] == 0.0

    def test_near_zero_price_sane(self):
        costs = fill_cost(1e-9, 100.0, False, CostModel.defaults())
        assert 0.0 <= costs["slippage"] < 1e-9
        assert costs["total"] >= 0.0

    def test_positive_price_unchanged(self):
        costs = fill_cost(20.0, 2.0, True, CostModel.defaults())
        assert costs["slippage"] == 20.0 * 2.0 * 5.0 / 10_000.0


class TestAdverseDirection:
    """Adverse = worse for the trader, by action sign, never price sign."""

    def test_buy_on_negative_print_pays_more(self):
        h = SimulatedExecutionHandler()
        (fill,) = h.fill([_order(OrderAction.BUY, True)], {"CL=F": _bars()[4]})
        assert fill.price > -37.63  # adverse: pays more than the print
        assert fill.slippage > 0

    def test_sell_on_negative_print_receives_less(self):
        h = SimulatedExecutionHandler()
        (fill,) = h.fill([_order(OrderAction.SELL, False)], {"CL=F": _bars()[4]})
        assert fill.price < -37.63  # adverse: receives less than the print
        assert fill.slippage > 0

    def test_buy_on_positive_print_still_lifts(self):
        h = SimulatedExecutionHandler()
        (fill,) = h.fill([_order(OrderAction.BUY, True)], {"CL=F": _bars()[0]})
        assert fill.price > 20.0

    def test_sell_on_positive_print_still_hits(self):
        h = SimulatedExecutionHandler()
        (fill,) = h.fill([_order(OrderAction.SELL, False)], {"CL=F": _bars()[0]})
        assert fill.price < 20.0

    def test_buy_on_zero_print_no_crash(self):
        h = SimulatedExecutionHandler()
        zbar = Bar(
            symbol="CL=F",
            timestamp=BASE,
            open=0.0,
            high=0.5,
            low=-0.5,
            close=0.0,
            volume=100,
        )
        (fill,) = h.fill([_order(OrderAction.BUY, True)], {"CL=F": zbar})
        assert fill.price == 0.0 and fill.slippage == 0.0


class TestPercentCommission:
    def test_negative_price_commission_non_negative(self):
        pc = PercentCommission(0.0005)
        assert pc.cost(1.0, -37.63) == 0.0005 * 1.0 * 37.63
        assert pc.cost(1.0, -37.63) > 0


class _BuyAndHoldThroughCrash(Strategy):
    """LONG on bar 0, EXIT on the last bar -- holds through the print."""

    def __init__(self, n_bars: int):
        super().__init__(["CL=F"])
        self._n = n_bars
        self._seen = 0

    def on_bar(self, timestamp, bars):
        self._seen += 1
        if self._seen == 1:
            return [Signal("CL=F", timestamp, SignalAction.LONG)]
        if self._seen == self._n - 1:
            # Exit one bar early: fills happen on the bar *after* the signal.
            return [Signal("CL=F", timestamp, SignalAction.EXIT)]
        return []


class TestEngineAcrossNegativePrint:
    def test_no_crash_and_sane_accounting(self):
        bars = _bars()
        engine = BacktestEngine(
            data=ListDataHandler(bars),
            strategy=_BuyAndHoldThroughCrash(len(bars)),
            portfolio=Portfolio(100_000.0, FixedQuantitySizer(10)),
            adjustment_basis="none",
        )
        result = engine.run()  # must not raise
        assert result.num_trades >= 1
        for trade in result.trades:
            assert math.isfinite(trade.pnl)
            assert trade.commission >= 0
        # Bought ~10 units near 20, sold near 12: a real loss, finite equity.
        assert result.final_equity < 100_000.0
        assert result.final_equity > 0
        for pt in result.equity_curve:
            assert math.isfinite(pt.equity)  # no NaN/inf in the curve

    def test_all_fill_cost_legs_non_negative(self):
        class _Recording(SimulatedExecutionHandler):
            def __init__(self):
                super().__init__()
                self.seen: list = []

            def fill(self, orders, bars):
                fills = super().fill(orders, bars)
                self.seen.extend(fills)
                return fills

        recorder = _Recording()
        engine = BacktestEngine(
            data=ListDataHandler(_bars()),
            strategy=_BuyAndHoldThroughCrash(len(CLOSES)),
            portfolio=Portfolio(100_000.0, FixedQuantitySizer(10)),
            execution=recorder,
            adjustment_basis="none",
        )
        engine.run()
        assert recorder.seen, "expected fills across the negative print"
        for fill in recorder.seen:
            assert fill.slippage >= 0
            assert fill.spread_cost >= 0
            assert fill.commission >= 0
            assert math.isfinite(fill.total_cost)
