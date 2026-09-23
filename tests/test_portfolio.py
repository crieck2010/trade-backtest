"""Portfolio, sizers, and FIFO trade reconstruction."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isclose

import pytest

from trade_backtest import (
    Bar,
    Fill,
    FixedQuantitySizer,
    OrderAction,
    PercentEquitySizer,
    Portfolio,
    PortfolioError,
    Signal,
    SignalAction,
    normalize_bar,
)

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 6, tzinfo=timezone.utc)
T2 = datetime(2026, 1, 7, tzinfo=timezone.utc)


def bar(close: float, symbol: str = "TEST") -> Bar:
    return Bar(symbol=symbol, timestamp=T0, open=close, high=close,
               low=close, close=close, volume=100)


def signal(action: SignalAction, symbol: str = "TEST") -> Signal:
    return Signal(symbol=symbol, timestamp=T0, action=action)


def make_portfolio(sizer=None) -> Portfolio:
    return Portfolio(10_000.0, sizer or FixedQuantitySizer(10))


# -- signals -> orders ---------------------------------------------------------


def test_long_signal_produces_buy_order():
    p = make_portfolio()
    order = p.on_signal(signal(SignalAction.LONG), {"TEST": bar(100.0)})
    assert order is not None
    assert order.action is OrderAction.BUY
    assert order.quantity == 10


def test_exit_with_no_position_produces_no_order():
    p = make_portfolio()
    assert p.on_signal(signal(SignalAction.EXIT), {"TEST": bar(100.0)}) is None


def test_second_long_signal_produces_no_order():
    p = make_portfolio()
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.BUY,
                   quantity=10, price=100.0))
    assert p.on_signal(signal(SignalAction.LONG), {"TEST": bar(100.0)}) is None


def test_exit_flattens_position():
    p = make_portfolio()
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.BUY,
                   quantity=10, price=100.0))
    order = p.on_signal(signal(SignalAction.EXIT), {"TEST": bar(110.0)})
    assert order is not None
    assert order.action is OrderAction.SELL
    assert order.quantity == 10


def test_short_then_long_reverses():
    p = make_portfolio()
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.SELL,
                   quantity=10, price=100.0))
    order = p.on_signal(signal(SignalAction.LONG), {"TEST": bar(100.0)})
    assert order.action is OrderAction.BUY
    assert order.quantity == 20  # cover 10 short + 10 long


def test_unknown_symbol_raises():
    p = make_portfolio()
    with pytest.raises(PortfolioError):
        p.on_signal(signal(SignalAction.LONG, symbol="NOPE"), {"TEST": bar(100.0)})


def test_limit_signal_makes_limit_order():
    p = make_portfolio()
    sig = Signal(symbol="TEST", timestamp=T0, action=SignalAction.LONG, limit_price=99.0)
    order = p.on_signal(sig, {"TEST": bar(100.0)})
    assert order is not None
    assert order.limit_price == 99.0


# -- sizers ----------------------------------------------------------------------


def test_fixed_quantity_sizer():
    p = make_portfolio(FixedQuantitySizer(5))
    assert p.sizer.size(signal(SignalAction.LONG), 100.0, p) == 5
    assert p.sizer.size(signal(SignalAction.SHORT), 100.0, p) == -5
    assert p.sizer.size(signal(SignalAction.EXIT), 100.0, p) == 0.0


def test_percent_equity_sizer():
    p = make_portfolio(PercentEquitySizer(0.5))
    assert p.sizer.size(signal(SignalAction.LONG), 100.0, p) == 50.0
    assert p.sizer.size(signal(SignalAction.SHORT), 100.0, p) == -50.0
    assert p.sizer.size(signal(SignalAction.EXIT), 100.0, p) == 0.0


def test_percent_equity_sizer_caps_leverage():
    p = make_portfolio(PercentEquitySizer(0.9, max_leverage=0.5))
    assert p.sizer.size(signal(SignalAction.LONG), 100.0, p) == 50.0


def test_strength_scales_quantity():
    p = make_portfolio(FixedQuantitySizer(10))
    sig = Signal(symbol="TEST", timestamp=T0, action=SignalAction.LONG, strength=0.5)
    assert p.sizer.size(sig, 100.0, p) == 5.0


# -- fills, FIFO lots, trades -------------------------------------------------------


def test_fifo_trade_reconstruction():
    p = make_portfolio()
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.BUY, quantity=10, price=100.0))
    p.on_fill(Fill(symbol="TEST", timestamp=T1, action=OrderAction.BUY, quantity=10, price=110.0))
    p.on_fill(Fill(symbol="TEST", timestamp=T2, action=OrderAction.SELL, quantity=15, price=120.0))
    assert len(p.trades) == 2
    assert p.trades[0].quantity == 10 and p.trades[0].pnl == 200.0
    assert p.trades[0].entry_price == 100.0 and p.trades[0].exit_price == 120.0
    assert p.trades[1].quantity == 5 and p.trades[1].pnl == 50.0
    assert p.position("TEST") == 5
    # cash: 10000 - 1000 - 1100 + 1800
    assert isclose(p.cash, 9_700.0, rel_tol=1e-9)


def test_short_cover_trade_pnl():
    p = make_portfolio()
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.SELL, quantity=10, price=100.0))
    p.on_fill(Fill(symbol="TEST", timestamp=T1, action=OrderAction.BUY, quantity=10, price=90.0))
    assert len(p.trades) == 1
    assert p.trades[0].pnl == 100.0
    assert p.position("TEST") == 0


def test_commission_split_across_legs():
    p = make_portfolio()
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.BUY, quantity=10, price=100.0))
    p.on_fill(Fill(symbol="TEST", timestamp=T1, action=OrderAction.BUY, quantity=10, price=110.0))
    p.on_fill(Fill(symbol="TEST", timestamp=T2, action=OrderAction.SELL,
                   quantity=20, price=120.0, commission=10.0))
    assert len(p.trades) == 2
    assert isclose(p.trades[0].commission + p.trades[1].commission, 10.0, rel_tol=1e-9)


def test_mark_to_market_and_record():
    p = make_portfolio()
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.BUY, quantity=10, price=100.0))
    p.mark_to_market(T1, {"TEST": bar(120.0)})
    assert isclose(p.equity, 9_000.0 + 1_200.0, rel_tol=1e-9)
    p.record(T1)
    assert len(p.equity_curve) == 1
    assert isclose(p.equity_curve[0].equity, 10_200.0, rel_tol=1e-9)


def test_normalize_bar_feeds_portfolio():
    raw = {"symbol": "test", "timestamp": "2026-01-05T00:00:00+00:00",
           "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}
    assert normalize_bar(raw).symbol == "TEST"
