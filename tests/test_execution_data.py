"""Execution handler, commissions, slippage, and data handler tests."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isclose

import pytest

from trade_backtest import (
    Bar,
    DataError,
    FlatCommission,
    ListDataHandler,
    NoCommission,
    Order,
    OrderAction,
    OrderType,
    PercentCommission,
    PerShareCommission,
    SimulatedExecutionHandler,
    normalize_bar,
)

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def bar(symbol="TEST", open=100.0, high=102.0, low=98.0, close=101.0) -> Bar:
    return Bar(symbol=symbol, timestamp=T0, open=open, high=high,
               low=low, close=close, volume=1000)


def market_buy(symbol="TEST", qty=10.0) -> Order:
    return Order(symbol=symbol, timestamp=T0, action=OrderAction.BUY, quantity=qty)


# -- commissions -------------------------------------------------------------------


def test_flat_commission():
    assert FlatCommission(2.5).cost(100, 50.0) == 2.5


def test_per_share_commission():
    assert isclose(PerShareCommission(0.005).cost(100, 50.0), 0.5)


def test_percent_commission():
    assert isclose(PercentCommission(0.001).cost(100, 50.0), 5.0)


def test_negative_commission_rejected():
    with pytest.raises(ValueError):
        FlatCommission(-1.0)


# -- fills ---------------------------------------------------------------------------


def test_market_buy_fills_at_open():
    ex = SimulatedExecutionHandler()
    fills = ex.fill([market_buy()], {"TEST": bar(open=100.0)})
    assert len(fills) == 1
    assert fills[0].price == 100.0
    assert fills[0].commission == 0.0


def test_slippage_is_adverse():
    ex = SimulatedExecutionHandler(slippage_bps=100.0)  # 1%
    buy = ex.fill([market_buy()], {"TEST": bar(open=100.0)})[0]
    sell = ex.fill([Order(symbol="TEST", timestamp=T0, action=OrderAction.SELL, quantity=10)],
                   {"TEST": bar(open=100.0)})[0]
    assert isclose(buy.price, 101.0)
    assert isclose(sell.price, 99.0)


def test_commission_attached_to_fill():
    ex = SimulatedExecutionHandler(commission=PerShareCommission(0.01))
    fills = ex.fill([market_buy(qty=10.0)], {"TEST": bar(open=100.0)})
    assert isclose(fills[0].commission, 0.10)


def test_limit_buy_fills_when_touched():
    ex = SimulatedExecutionHandler()
    order = Order(symbol="TEST", timestamp=T0, action=OrderAction.BUY, quantity=10,
                  order_type=OrderType.LIMIT, limit_price=99.0)
    fills = ex.fill([order], {"TEST": bar(open=100.0, low=98.0)})
    assert len(fills) == 1
    assert fills[0].price == 99.0  # min(limit, open)


def test_limit_buy_skipped_when_not_touched():
    ex = SimulatedExecutionHandler()
    order = Order(symbol="TEST", timestamp=T0, action=OrderAction.BUY, quantity=10,
                  order_type=OrderType.LIMIT, limit_price=97.0)
    assert ex.fill([order], {"TEST": bar(open=100.0, low=98.0)}) == []


def test_limit_sell_fills_when_touched():
    ex = SimulatedExecutionHandler()
    order = Order(symbol="TEST", timestamp=T0, action=OrderAction.SELL, quantity=10,
                  order_type=OrderType.LIMIT, limit_price=101.0)
    fills = ex.fill([order], {"TEST": bar(open=100.0, high=102.0)})
    assert len(fills) == 1
    assert fills[0].price == 101.0  # max(limit, open)


def test_missing_symbol_order_lapses():
    ex = SimulatedExecutionHandler()
    assert ex.fill([market_buy(symbol="MISSING")], {"TEST": bar()}) == []


# -- data handler -----------------------------------------------------------------------


def test_normalize_bar_from_dict():
    b = normalize_bar({"symbol": "aapl", "timestamp": T0, "open": 1, "high": 2,
                       "low": 0.5, "close": 1.5, "volume": 10})
    assert b.symbol == "AAPL"
    assert b.close == 1.5


def test_normalize_bar_from_object():
    class FakeBar:
        symbol = "BTC/USD"
        timestamp = T0
        open, high, low, close, volume = 1, 2, 0.5, 1.5, 10
    b = normalize_bar(FakeBar())
    assert b.close == 1.5


def test_normalize_bar_iso_timestamp():
    b = normalize_bar({"symbol": "X", "timestamp": "2026-01-05T00:00:00+00:00",
                       "open": 1, "high": 1, "low": 1, "close": 1})
    assert b.timestamp == T0


def test_normalize_bar_missing_symbol_raises():
    with pytest.raises(DataError):
        normalize_bar({"timestamp": T0, "open": 1, "high": 1, "low": 1, "close": 1})


def test_list_handler_groups_and_sorts():
    bars = [
        Bar(symbol="B", timestamp=T0, open=1, high=1, low=1, close=1),
        Bar(symbol="A", timestamp=T0, open=1, high=1, low=1, close=1),
    ]
    handler = ListDataHandler(list(reversed(bars)))
    assert handler.symbols == ["A", "B"]
    streamed = list(handler.stream())
    assert len(streamed) == 1
    ts, bucket = streamed[0]
    assert ts == T0 and set(bucket) == {"A", "B"}


def test_list_handler_rejects_duplicates():
    with pytest.raises(DataError):
        list(ListDataHandler([bar(), bar()]).stream())


def test_list_handler_rejects_empty():
    with pytest.raises(DataError):
        ListDataHandler([])
