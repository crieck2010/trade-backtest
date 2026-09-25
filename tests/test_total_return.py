"""Total return: dividends, splits, adjustment basis, benchmarks.

Every number here is hand-computed. The point of the module is that
price-return backtests lie quietly; these tests pin the lie and the fix.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isclose

import pytest

from trade_backtest import (
    AdjustedDataHandler,
    BacktestEngine,
    BacktestError,
    Bar,
    CorporateAction,
    CostModel,
    DataError,
    Dividend,
    FixedQuantitySizer,
    ListDataHandler,
    NoCommission,
    OrderAction,
    Portfolio,
    Signal,
    SignalAction,
    SimulatedExecutionHandler,
    Split,
    Strategy,
    buy_and_hold_curve,
    excess_vs_benchmark,
    normalize_bar,
)
from trade_backtest import Fill

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def flat_bars(symbol="TEST", n=10, price=100.0, start=T0):
    return [
        normalize_bar({
            "symbol": symbol,
            "timestamp": start + timedelta(days=i),
            "open": price, "high": price, "low": price,
            "close": price, "volume": 1000,
        })
        for i in range(n)
    ]


class BuyOnceHold(Strategy):
    """Buy on the first bar, hold forever."""

    def __init__(self, action=SignalAction.LONG):
        super().__init__(["TEST"])
        self._action = action
        self._done = False

    def on_bar(self, timestamp, bars):
        if self._done:
            return []
        self._done = True
        return [Signal("TEST", timestamp, self._action)]


def zero_cost_engine(bars, strategy, corporate_actions=(), basis="unadjusted_with_events",
                     dividend_reinvestment=True):
    """Engine with every cost leg zeroed, so dividend/split arithmetic is exact."""
    return BacktestEngine(
        data=ListDataHandler(bars),
        strategy=strategy,
        portfolio=Portfolio(
            10_000.0, FixedQuantitySizer(100),
            cost_model=CostModel.disabled("isolate corporate-action arithmetic"),
            dividend_reinvestment=dividend_reinvestment,
        ),
        execution=SimulatedExecutionHandler(slippage_bps=0.0, commission=NoCommission()),
        adjustment_basis=basis,
        corporate_actions=list(corporate_actions),
    )


# -- dividends -----------------------------------------------------------------


def test_dividend_caught_total_return_not_price_return():
    """Buy 100 sh @ $100, $2/sh dividend on day 5, price flat.

    Price return is 0%; total return is 2.0%. A backtest that ignores the
    dividend reports 0% -- the quiet lie. Hand-computed: $200 payout
    reinvested at the $100 close -> 102 sh -> $10,200 on $10,000.
    """
    div = Dividend("TEST", (T0 + timedelta(days=4)).date(), 2.0)
    engine = zero_cost_engine(flat_bars(), BuyOnceHold(), corporate_actions=[div])
    result = engine.run()
    assert isclose(result.metrics["total_return"], 0.02, rel_tol=1e-12)
    assert isclose(result.final_equity, 10_200.0, rel_tol=1e-12)
    assert result.assumptions["corporate_actions_applied"] == {"dividends": 1, "splits": 0}


def test_dividend_reinvestment_increases_share_count():
    div = Dividend("TEST", (T0 + timedelta(days=4)).date(), 2.0)
    engine = zero_cost_engine(flat_bars(), BuyOnceHold(), corporate_actions=[div])
    engine.run()
    assert isclose(engine.portfolio.position("TEST"), 102.0, rel_tol=1e-12)


def test_dividend_cash_when_reinvestment_off():
    div = Dividend("TEST", (T0 + timedelta(days=4)).date(), 2.0)
    engine = zero_cost_engine(flat_bars(), BuyOnceHold(), corporate_actions=[div],
                              dividend_reinvestment=False)
    result = engine.run()
    assert isclose(engine.portfolio.position("TEST"), 100.0, rel_tol=1e-12)
    assert isclose(engine.portfolio.cash, 200.0, rel_tol=1e-12)
    assert isclose(result.final_equity, 10_200.0, rel_tol=1e-12)


def test_short_position_pays_the_dividend():
    """Short 100 sh @ $100; $2 dividend -> cash debited $200."""
    div = Dividend("TEST", (T0 + timedelta(days=4)).date(), 2.0)
    engine = zero_cost_engine(
        flat_bars(), BuyOnceHold(action=SignalAction.SHORT), corporate_actions=[div]
    )
    result = engine.run()
    # 10000 + 100*100 proceeds, minus the $200 dividend paid to the lender.
    assert isclose(engine.portfolio.cash, 19_800.0, rel_tol=1e-12)
    assert isclose(result.final_equity, 9_800.0, rel_tol=1e-12)


# -- splits --------------------------------------------------------------------


def test_split_rescales_lots_position_value_unchanged():
    """2:1 split: 100 sh @ $100 -> 200 sh @ $50; value unchanged."""
    p = Portfolio(10_000.0, FixedQuantitySizer(10),
                  cost_model=CostModel.disabled("unit test"))
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.BUY,
                   quantity=100, price=100.0))
    desc = p.apply_corporate_action(Split("TEST", T0.date(), 2.0))
    assert "2-for-1" in desc
    assert p.position("TEST") == 200
    lots = p._lots["TEST"]
    assert len(lots) == 1 and lots[0][0] == 200 and lots[0][1] == 50.0
    p.mark_to_market(T0, {"TEST": Bar(symbol="TEST", timestamp=T0, open=50, high=50,
                                      low=50, close=50, volume=2000)})
    assert isclose(p.equity, 10_000.0, rel_tol=1e-12)


def test_split_fifo_pnl_unaffected():
    """A split must not create or destroy P&L: buy 100 @ $100, 2:1 split,
    sell 200 @ $60 post-split -> (60-50)*200 = $2000, the same as the
    unadjusted economics (100 @ $100 -> 100 @ $120)."""
    p = Portfolio(10_000.0, FixedQuantitySizer(10),
                  cost_model=CostModel.disabled("unit test"))
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.BUY,
                   quantity=100, price=100.0))
    p.apply_corporate_action(Split("TEST", T0.date(), 2.0))
    p.on_fill(Fill(symbol="TEST", timestamp=T0, action=OrderAction.SELL,
                   quantity=200, price=60.0))
    assert len(p.trades) == 1
    trade = p.trades[0]
    assert trade.quantity == 200
    assert trade.entry_price == 50.0 and trade.exit_price == 60.0
    assert isclose(trade.pnl, 2_000.0, rel_tol=1e-12)


def test_adjusted_data_handler_halves_pre_split_bars():
    pre = normalize_bar({"symbol": "TEST", "timestamp": T0, "open": 100, "high": 101,
                         "low": 99, "close": 100, "volume": 1000})
    post = normalize_bar({"symbol": "TEST", "timestamp": T0 + timedelta(days=10),
                          "open": 50, "high": 51, "low": 49, "close": 50, "volume": 3000})
    split = Split("TEST", (T0 + timedelta(days=5)).date(), 2.0)
    handler = AdjustedDataHandler(ListDataHandler([pre, post]), [split])
    assert handler.symbols == ["TEST"]
    _, bucket = list(handler.stream())[0]
    adj = bucket["TEST"]
    assert adj.open == 50.0 and adj.close == 50.0
    assert adj.volume == 2000  # volume scales inversely
    _, bucket = list(handler.stream())[1]
    untouched = bucket["TEST"]
    assert untouched.open == 50.0 and untouched.volume == 3000  # post-ex-date: untouched


def test_multiple_splits_compound():
    pre = normalize_bar({"symbol": "TEST", "timestamp": T0, "open": 400, "high": 400,
                         "low": 400, "close": 400, "volume": 100})
    later = normalize_bar({"symbol": "TEST", "timestamp": T0 + timedelta(days=30),
                           "open": 100, "high": 100, "low": 100, "close": 100,
                           "volume": 100})
    splits = [Split("TEST", (T0 + timedelta(days=10)).date(), 2.0),
              Split("TEST", (T0 + timedelta(days=20)).date(), 2.0)]
    handler = AdjustedDataHandler(ListDataHandler([pre, later]), splits)
    adj = list(handler.stream())[0][1]["TEST"]
    assert adj.close == 100.0  # 400 / (2*2)
    assert adj.volume == 400


# -- basis validation ------------------------------------------------------------


def _minimal_engine_kwargs(**overrides):
    kw = dict(
        data=ListDataHandler(flat_bars(n=3)),
        strategy=BuyOnceHold(),
        portfolio=Portfolio(10_000.0, FixedQuantitySizer(10)),
    )
    kw.update(overrides)
    return kw


def test_missing_adjustment_basis_raises():
    with pytest.raises(BacktestError):
        BacktestEngine(**_minimal_engine_kwargs())


def test_none_adjustment_basis_raises():
    with pytest.raises(BacktestError):
        BacktestEngine(**_minimal_engine_kwargs(adjustment_basis=None))


def test_unknown_adjustment_basis_raises():
    with pytest.raises(BacktestError):
        BacktestEngine(**_minimal_engine_kwargs(adjustment_basis="yolo"))


def test_corporate_actions_require_unadjusted_basis():
    div = Dividend("TEST", T0.date(), 1.0)
    with pytest.raises(BacktestError):
        BacktestEngine(**_minimal_engine_kwargs(
            adjustment_basis="pre_adjusted", corporate_actions=[div]))
    with pytest.raises(BacktestError):
        BacktestEngine(**_minimal_engine_kwargs(
            adjustment_basis="none", corporate_actions=[div]))


def test_dividend_action_validation():
    with pytest.raises(ValueError):
        Dividend("TEST", T0.date(), -1.0)
    with pytest.raises(ValueError):
        Split("TEST", T0.date(), 0.0)
    assert isinstance(Dividend("t", T0.date(), 1.0), CorporateAction)
    assert Dividend("t", T0.date(), 1.0).symbol == "T"


# -- benchmark -----------------------------------------------------------------


def test_buy_and_hold_matches_hand_computed_total_return():
    """100 sh @ $100 open; $2 dividend ex day 5 reinvested at $100 close ->
    102 sh -> $10,200. The price-only benchmark stays at $10,000: the
    difference is exactly the dividend yield effect ($200/$10,000)."""
    div = Dividend("TEST", (T0 + timedelta(days=4)).date(), 2.0)
    bars = flat_bars()
    tr_curve = buy_and_hold_curve(bars, 10_000.0, "unadjusted_with_events", [div])
    pr_curve = buy_and_hold_curve(bars, 10_000.0, "pre_adjusted")
    assert isclose(tr_curve[-1].equity, 10_200.0, rel_tol=1e-12)
    assert isclose(pr_curve[-1].equity, 10_000.0, rel_tol=1e-12)
    assert len(tr_curve) == len(bars)
    result = excess_vs_benchmark(tr_curve, pr_curve)
    assert isclose(result["benchmark_total_return"], 0.0, abs_tol=1e-12)
    assert isclose(result["strategy_total_return"], 0.02, rel_tol=1e-12)
    assert isclose(result["excess_return"], 0.02, rel_tol=1e-12)  # the dividend yield effect


def test_buy_and_hold_rejects_misuse():
    bars = flat_bars()
    div = Dividend("TEST", T0.date(), 1.0)
    with pytest.raises(BacktestError):
        buy_and_hold_curve(bars, 10_000.0, "pre_adjusted", [div])
    with pytest.raises(BacktestError):
        buy_and_hold_curve(bars, 10_000.0, "bogus")
    with pytest.raises(DataError):
        buy_and_hold_curve([], 10_000.0, "pre_adjusted")


def test_excess_vs_benchmark_identical_curves_is_zero():
    curve = buy_and_hold_curve(flat_bars(), 10_000.0, "pre_adjusted")
    result = excess_vs_benchmark(curve, curve)
    assert result == {"strategy_total_return": 0.0, "benchmark_total_return": 0.0,
                      "excess_return": 0.0}


def test_excess_vs_benchmark_rejects_misaligned():
    a = buy_and_hold_curve(flat_bars(), 10_000.0, "pre_adjusted")
    b = buy_and_hold_curve(flat_bars(n=5), 10_000.0, "pre_adjusted")
    with pytest.raises(BacktestError):
        excess_vs_benchmark(a, b)
    shifted = buy_and_hold_curve(flat_bars(start=T0 + timedelta(days=1)), 10_000.0,
                                 "pre_adjusted")
    with pytest.raises(BacktestError):
        excess_vs_benchmark(a, shifted)


# -- assumptions block -----------------------------------------------------------


def test_assumptions_block_has_all_required_keys():
    div = Dividend("TEST", (T0 + timedelta(days=4)).date(), 2.0)
    engine = zero_cost_engine(flat_bars(), BuyOnceHold(), corporate_actions=[div])
    result = engine.run()
    a = result.assumptions
    for key in ("engine_version", "adjustment_basis", "adjustment_note",
                "corporate_actions_applied", "dividend_reinvestment",
                "cost_model", "borrow", "not_modeled", "periods_per_year"):
        assert key in a, f"missing assumptions key: {key}"
    assert a["engine_version"] == "0.2.0"
    assert a["adjustment_basis"] == "unadjusted_with_events"
    assert a["corporate_actions_applied"] == {"dividends": 1, "splits": 0}
    assert a["dividend_reinvestment"] is True
    assert a["borrow"]["day_count"] == "actual/365"
    assert a["borrow"]["total_paid"] == 0.0
    assert len(a["not_modeled"]) > 0
    cm = a["cost_model"]
    assert cm["disabled"] is True
    assert cm["disabled_reason"] == "isolate corporate-action arithmetic"
