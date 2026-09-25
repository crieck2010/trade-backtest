"""Costs: decomposition, entry/exit knobs, borrow, default-on, legacy, opt-out.

Hand-computed throughout. The v0.2.0 contract: costs are default-on, the
opt-out is explicit and reasoned, and the legacy path keeps old explicit
callers (e.g. trade-swing's post-hoc cost model) at exactly zero cost.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isclose

import pytest

from trade_backtest import (
    BacktestEngine,
    CostModel,
    FixedQuantitySizer,
    ListDataHandler,
    NoCommission,
    Order,
    OrderAction,
    OrderType,
    Portfolio,
    Signal,
    SignalAction,
    SimulatedExecutionHandler,
    Strategy,
    normalize_bar,
)
from trade_backtest import Fill
from trade_backtest.costs import fill_cost

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def bar(open=100.0):
    return normalize_bar({"symbol": "TEST", "timestamp": T0, "open": open,
                          "high": open + 2, "low": open - 2,
                          "close": open, "volume": 1000})


def buy_order(qty=100.0, is_entry=True):
    return Order(symbol="TEST", timestamp=T0, action=OrderAction.BUY,
                 quantity=qty, is_entry=is_entry)


def sell_order(qty=100.0, is_entry=False):
    return Order(symbol="TEST", timestamp=T0, action=OrderAction.SELL,
                 quantity=qty, is_entry=is_entry)


# -- decomposition ---------------------------------------------------------------


def test_cost_decomposition_hand_computed():
    """100 sh @ $50, default model: slippage 5 bps = $2.50, half-spread
    1 bp = $0.50, per-share commission $0.50 -> total $3.50."""
    model = CostModel.defaults()
    parts = fill_cost(50.0, 100.0, True, model)
    assert isclose(parts["slippage"], 2.50, rel_tol=1e-12)
    assert isclose(parts["spread"], 0.50, rel_tol=1e-12)
    assert isclose(parts["commission"], 0.50, rel_tol=1e-12)
    assert isclose(parts["total"], 3.50, rel_tol=1e-12)


def test_fill_carries_decomposition_and_total_cost():
    """End to end through execution: a bare (default-on) handler prices a
    100 sh @ $50 buy with slippage 5 bps / spread 1 bp / $0.005 per share."""
    ex = SimulatedExecutionHandler()  # default-on
    (fill,) = ex.fill([buy_order()], {"TEST": bar(open=50.0)})
    assert isclose(fill.price, 50.0 * 1.0005, rel_tol=1e-12)  # slippage in the price
    assert isclose(fill.slippage, 2.50, rel_tol=1e-12)
    assert isclose(fill.spread_cost, 0.50, rel_tol=1e-12)
    assert isclose(fill.commission, 0.50, rel_tol=1e-12)
    assert isclose(fill.total_cost, 3.50, rel_tol=1e-12)


def test_fill_defaults_keep_old_constructions_working():
    f = Fill(symbol="TEST", timestamp=T0, action=OrderAction.BUY,
             quantity=10, price=100.0, commission=1.0)
    assert f.slippage == 0.0 and f.spread_cost == 0.0
    assert f.total_cost == 1.0
    o = Order(symbol="TEST", timestamp=T0, action=OrderAction.BUY, quantity=10)
    assert o.is_entry is True


def test_entry_vs_exit_slippage_knobs_applied_separately():
    """Entry 5 bps vs exit 20 bps: the closing fill is hit harder."""
    model = CostModel(commission=NoCommission(), slippage_entry_bps=5.0,
                      slippage_exit_bps=20.0, half_spread_bps=0.0,
                      borrow_cost_annual_bps=0.0)
    ex = SimulatedExecutionHandler(cost_model=model)
    (entry,) = ex.fill([buy_order(is_entry=True)], {"TEST": bar(open=100.0)})
    (exit_,) = ex.fill([sell_order(is_entry=False)], {"TEST": bar(open=100.0)})
    assert isclose(entry.price, 100.05, rel_tol=1e-12)
    assert isclose(exit_.price, 99.80, rel_tol=1e-12)
    assert isclose(entry.slippage, 0.05 * 100, rel_tol=1e-12)
    assert isclose(exit_.slippage, 0.20 * 100, rel_tol=1e-12)


def test_spread_charged_to_cash_not_baked_into_price():
    """The half-spread never moves the fill price; the portfolio debits it
    from cash on top of price + commission."""
    ex = SimulatedExecutionHandler()  # default-on
    p = Portfolio(10_000.0, FixedQuantitySizer(100))
    (fill,) = ex.fill([buy_order()], {"TEST": bar(open=50.0)})
    p.on_fill(fill)
    # cash = 10000 - 100*50.025 (price incl. slippage) - 0.50 commission - 0.50 spread
    assert isclose(p.cash, 10_000.0 - 5_002.50 - 0.50 - 0.50, rel_tol=1e-9)
    assert isclose(fill.price, 50.025, rel_tol=1e-12)  # spread not in price


# -- borrow --------------------------------------------------------------------


def test_borrow_accrual_hand_computed():
    """Short $10,000 notional, 50 bps/yr, 10 days:
    10000 * 0.005 * 10/365 = $1.36986..."""
    p = Portfolio(10_000.0, FixedQuantitySizer(100))  # default cost model
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.SELL,
                   quantity=100, price=100.0))
    t1 = T0 + timedelta(days=10)
    cost = p.accrue_borrow_cost(t1, T0)
    assert isclose(cost, 500.0 / 365.0, rel_tol=1e-12)
    assert isclose(p.total_borrow_cost, 500.0 / 365.0, rel_tol=1e-12)
    assert isclose(p.cash, 20_000.0 - 500.0 / 365.0, rel_tol=1e-9)


def test_borrow_accrual_skips_without_shorts_or_time():
    p = Portfolio(10_000.0, FixedQuantitySizer(100))
    assert p.accrue_borrow_cost(T0, None) == 0.0  # no previous timestamp
    assert p.accrue_borrow_cost(T0, T0) == 0.0  # no elapsed time
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.BUY,
                   quantity=100, price=100.0))
    assert p.accrue_borrow_cost(T0 + timedelta(days=10), T0) == 0.0  # long only


def test_opt_out_short_accrues_no_borrow():
    p = Portfolio(10_000.0, FixedQuantitySizer(100),
                  cost_model=CostModel.disabled("borrow not modeled in this test"))
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.SELL,
                   quantity=100, price=100.0))
    assert p.accrue_borrow_cost(T0 + timedelta(days=10), T0) == 0.0
    assert p.total_borrow_cost == 0.0
    assert p.cash == 20_000.0


# -- default-on vs opt-out vs legacy --------------------------------------------


class BuyOnce(Strategy):
    def __init__(self):
        super().__init__(["TEST"])
        self._done = False

    def on_bar(self, timestamp, bars):
        if self._done:
            return []
        self._done = True
        return [Signal("TEST", timestamp, SignalAction.LONG)]


def trend_bars(n=10):
    return [
        normalize_bar({"symbol": "TEST", "timestamp": T0 + timedelta(days=i),
                       "open": 100 + i, "high": 101 + i, "low": 99 + i,
                       "close": 100 + i, "volume": 1000})
        for i in range(n)
    ]


def test_default_on_bare_engine_applies_costs():
    """A bare BacktestEngine (no cost args anywhere) must end below an
    otherwise identical zero-cost run."""
    bare = BacktestEngine(
        data=ListDataHandler(trend_bars()), strategy=BuyOnce(),
        portfolio=Portfolio(10_000.0, FixedQuantitySizer(10)),
        adjustment_basis="none",
    ).run()
    zero = BacktestEngine(
        data=ListDataHandler(trend_bars()), strategy=BuyOnce(),
        portfolio=Portfolio(10_000.0, FixedQuantitySizer(10),
                            cost_model=CostModel.disabled("zero-cost comparison")),
        execution=SimulatedExecutionHandler(slippage_bps=0.0, commission=NoCommission()),
        adjustment_basis="none",
    ).run()
    assert bare.final_equity < zero.final_equity
    assert bare.assumptions["cost_model"]["disabled"] is False
    assert bare.assumptions["cost_model"]["slippage_entry_bps"] == 5.0


def test_explicit_opt_out_zero_costs_and_reason_in_assumptions():
    engine = BacktestEngine(
        data=ListDataHandler(trend_bars()), strategy=BuyOnce(),
        portfolio=Portfolio(10_000.0, FixedQuantitySizer(10),
                            cost_model=CostModel.disabled("unit test opt-out")),
        execution=SimulatedExecutionHandler(
            cost_model=CostModel.disabled("unit test opt-out")),
        adjustment_basis="none",
    )
    result = engine.run()
    cm = result.assumptions["cost_model"]
    assert cm["disabled"] is True
    assert cm["disabled_reason"] == "unit test opt-out"
    assert cm["slippage_entry_bps"] == 0.0
    assert cm["borrow_cost_annual_bps"] == 0.0
    assert result.assumptions["borrow"]["total_paid"] == 0.0


def test_legacy_mode_zero_fill_costs_and_visible_path():
    """What this test asserts, exactly:

    * fill-level: price == raw open, commission/slippage/spread all 0,
      total_cost 0 -- identical to pre-0.2.0 behavior;
    * the legacy path is visible in the cost model: ``disabled`` is True
      with a reason naming the explicit-args path;
    * borrow is portfolio-level, NOT execution-level: a legacy handler
      does not by itself disable borrow -- the portfolio's own
      cost_model governs that (asserted via a default portfolio still
      carrying 50 bps in its model).
    """
    ex = SimulatedExecutionHandler(slippage_bps=0.0, commission=NoCommission())
    assert ex.is_legacy is True
    (fill,) = ex.fill([buy_order()], {"TEST": bar(open=100.0)})
    assert fill.price == 100.0
    assert fill.commission == 0.0 and fill.slippage == 0.0 and fill.spread_cost == 0.0
    assert fill.total_cost == 0.0
    cm = ex.cost_model.as_dict()
    assert cm["disabled"] is True
    assert "legacy" in cm["disabled_reason"]
    # Borrow follows the portfolio's model, not the handler's:
    p = Portfolio(10_000.0, FixedQuantitySizer(10))
    assert p.cost_model.borrow_cost_annual_bps == 50.0


def test_legacy_slippage_sets_both_knobs():
    ex = SimulatedExecutionHandler(slippage_bps=100.0, commission=NoCommission())
    (buy,) = ex.fill([buy_order(is_entry=True)], {"TEST": bar(open=100.0)})
    (sell,) = ex.fill([sell_order(is_entry=False)], {"TEST": bar(open=100.0)})
    assert isclose(buy.price, 101.0, rel_tol=1e-12)
    assert isclose(sell.price, 99.0, rel_tol=1e-12)
    assert buy.spread_cost == 0.0  # legacy: no half-spread


def test_new_knobs_without_cost_model_layer_onto_defaults():
    ex = SimulatedExecutionHandler(slippage_entry_bps=10.0)
    assert ex.is_legacy is False
    assert ex.cost_model.slippage_entry_bps == 10.0
    assert ex.cost_model.slippage_exit_bps == 5.0  # default kept
    assert ex.cost_model.commission.cost(100, 50.0) == 0.5  # default kept


def test_cost_model_disabled_requires_reason():
    with pytest.raises(ValueError):
        CostModel.disabled("")
    with pytest.raises(ValueError):
        CostModel.disabled("   ")
    with pytest.raises(ValueError):
        CostModel(disabled_reason="")
    with pytest.raises(ValueError):
        CostModel(slippage_entry_bps=-1.0)
    with pytest.raises(ValueError):
        CostModel(borrow_cost_annual_bps=float("inf"))


# -- is_entry classification -----------------------------------------------------


def _portfolio_with(sizer_qty, filled_qty):
    p = Portfolio(10_000.0, FixedQuantitySizer(sizer_qty),
                  cost_model=CostModel.disabled("unit test"))
    if filled_qty:
        p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.BUY,
                       quantity=filled_qty, price=100.0))
    return p


def _order_after(p, action):
    bars = {"TEST": bar(open=100.0)}
    return p.on_signal(Signal("TEST", T0, action), bars)


def test_is_entry_opening_from_flat():
    p = _portfolio_with(10, 0)
    assert _order_after(p, SignalAction.LONG).is_entry is True


def test_is_entry_exit_to_flat():
    p = _portfolio_with(10, 10)
    order = _order_after(p, SignalAction.EXIT)
    assert order.action is OrderAction.SELL and order.is_entry is False


def test_is_entry_increase_vs_reduce():
    p = _portfolio_with(20, 10)  # long 10, target 20
    assert _order_after(p, SignalAction.LONG).is_entry is True
    p = _portfolio_with(10, 20)  # long 20, target 10
    assert _order_after(p, SignalAction.LONG).is_entry is False


def test_reversal_dominant_leg_rule():
    """Reversals pay both knobs in reality; the order is classified by the
    dominant leg (ties -> entry), documented on Order."""
    # Tie: long 10 -> short 10 (close 10, open 10) -> entry.
    p = _portfolio_with(10, 10)
    order = _order_after(p, SignalAction.SHORT)
    assert order.quantity == 20 and order.is_entry is True
    # Opening dominates: long 10 -> short 30 (close 10, open 30) -> entry.
    p = _portfolio_with(30, 10)
    order = _order_after(p, SignalAction.SHORT)
    assert order.quantity == 40 and order.is_entry is True
    # Closing dominates: long 30 -> short 10 (close 30, open 10) -> exit.
    p = _portfolio_with(10, 30)
    order = _order_after(p, SignalAction.SHORT)
    assert order.quantity == 40 and order.is_entry is False


# -- limits with costs -----------------------------------------------------------


def test_limit_order_fill_with_cost_fields_present():
    ex = SimulatedExecutionHandler()  # default-on
    order = Order(symbol="TEST", timestamp=T0, action=OrderAction.BUY, quantity=10,
                  order_type=OrderType.LIMIT, limit_price=99.0)
    (fill,) = ex.fill([order], {"TEST": bar(open=100.0)})
    # Touch logic unchanged: raw = min(limit, open) = 99, then entry slippage.
    assert isclose(fill.price, 99.0 * 1.0005, rel_tol=1e-12)
    assert isclose(fill.slippage, 99.0 * 10 * 5 / 10_000.0, rel_tol=1e-12)
    assert isclose(fill.spread_cost, 99.0 * 10 * 1 / 10_000.0, rel_tol=1e-12)
    assert isclose(fill.total_cost,
                   fill.commission + fill.slippage + fill.spread_cost, rel_tol=1e-12)
