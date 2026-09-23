"""Performance metrics on hand-computed series."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from math import isclose

from trade_backtest import (
    EquityPoint,
    Trade,
    annualized_volatility,
    cagr,
    calmar_ratio,
    expectancy,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    summarize,
    total_return,
    win_rate,
)

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def curve(values: list[float]) -> list[EquityPoint]:
    return [EquityPoint(timestamp=T0 + timedelta(days=i), equity=v, cash=v)
            for i, v in enumerate(values)]


def trade(pnl: float) -> Trade:
    return Trade(symbol="T", entry_time=T0, exit_time=T0, quantity=1,
                 entry_price=100, exit_price=100 + pnl, pnl=pnl)


def test_total_return():
    assert isclose(total_return(curve([100, 110, 105, 115])), 0.15)


def test_total_return_needs_two_points():
    assert total_return(curve([100])) == 0.0


def test_max_drawdown_and_duration():
    dd, duration = max_drawdown(curve([100, 110, 105, 115, 90, 95]))
    assert isclose(dd, (115 - 90) / 115, rel_tol=1e-9)
    assert duration == 1  # bars from the 115 peak to the 90 trough


def test_max_drawdown_flat_series():
    dd, duration = max_drawdown(curve([100, 100, 100]))
    assert dd == 0.0 and duration == 0


def test_sharpe_zero_volatility():
    assert sharpe_ratio(curve([100, 100, 100])) == 0.0


def test_sharpe_positive_for_noisy_gains():
    assert sharpe_ratio(curve([100, 101, 100.5, 102.5])) > 0


def test_sortino_ignores_upside():
    # Same upside, deeper downside -> lower sortino
    mild = sortino_ratio(curve([100, 110, 108, 118]))
    wild = sortino_ratio(curve([100, 110, 90, 118]))
    assert mild > wild


def test_cagr_doubles_in_one_year():
    assert isclose(cagr(curve([100.0] * 252 + [200.0]), 252), 1.0, rel_tol=1e-6)


def test_calmar_zero_without_drawdown():
    assert calmar_ratio(curve([100, 110, 120])) == 0.0


def test_win_rate_and_expectancy():
    trades = [trade(10), trade(-5), trade(5)]
    assert isclose(win_rate(trades), 2 / 3)
    assert isclose(expectancy(trades), 10 / 3)


def test_win_rate_no_trades():
    assert win_rate([]) == 0.0


def test_profit_factor():
    assert isclose(profit_factor([trade(10), trade(-5)]), 2.0)
    assert profit_factor([trade(10)]) == math.inf
    assert profit_factor([]) == 0.0


def test_annualized_volatility_zero_for_flat():
    assert annualized_volatility(curve([100, 100, 100])) == 0.0


def test_summarize_keys():
    metrics = summarize(curve([100, 110, 105]), [trade(5)])
    for key in ("total_return", "cagr", "sharpe_ratio", "sortino_ratio",
                "max_drawdown", "calmar_ratio", "num_trades", "win_rate",
                "profit_factor", "expectancy"):
        assert key in metrics
    assert metrics["num_trades"] == 1.0
