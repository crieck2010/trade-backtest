"""Performance metrics over an equity curve and closed trades.

All functions are pure: they take plain sequences and return floats.
``periods_per_year`` annualizes (252 daily, 52 weekly, 12 monthly,
8_760 hourly for crypto). Ratios use simple (not log) returns.
"""

from __future__ import annotations

import math
import statistics

from .models import EquityPoint, Trade


def _returns(equity: list[EquityPoint]) -> list[float]:
    rets: list[float] = []
    for prev, cur in zip(equity, equity[1:]):
        if prev.equity != 0:
            rets.append(cur.equity / prev.equity - 1.0)
    return rets


def total_return(equity: list[EquityPoint]) -> float:
    """(final / initial) - 1."""
    if len(equity) < 2:
        return 0.0
    initial = equity[0].equity
    return equity[-1].equity / initial - 1.0 if initial else 0.0


def cagr(equity: list[EquityPoint], periods_per_year: float = 252) -> float:
    """Compound annual growth rate."""
    if len(equity) < 2:
        return 0.0
    initial = equity[0].equity
    if initial <= 0 or equity[-1].equity <= 0:
        return 0.0
    years = (len(equity) - 1) / periods_per_year
    if years <= 0:
        return 0.0
    return (equity[-1].equity / initial) ** (1.0 / years) - 1.0


def annualized_volatility(equity: list[EquityPoint], periods_per_year: float = 252) -> float:
    rets = _returns(equity)
    if len(rets) < 2:
        return 0.0
    return statistics.pstdev(rets) * math.sqrt(periods_per_year)


def sharpe_ratio(
    equity: list[EquityPoint],
    periods_per_year: float = 252,
    risk_free: float = 0.0,
) -> float:
    """Annualized Sharpe on simple returns; 0 when volatility is zero."""
    rets = _returns(equity)
    if len(rets) < 2:
        return 0.0
    rf_per = (1 + risk_free) ** (1 / periods_per_year) - 1 if risk_free else 0.0
    excess = [r - rf_per for r in rets]
    vol = statistics.pstdev(excess)
    if vol == 0:
        return 0.0
    return statistics.fmean(excess) / vol * math.sqrt(periods_per_year)


def sortino_ratio(
    equity: list[EquityPoint],
    periods_per_year: float = 252,
    risk_free: float = 0.0,
) -> float:
    """Annualized Sortino (downside deviation only)."""
    rets = _returns(equity)
    if len(rets) < 2:
        return 0.0
    rf_per = (1 + risk_free) ** (1 / periods_per_year) - 1 if risk_free else 0.0
    excess = [r - rf_per for r in rets]
    downside = [min(0.0, r) for r in excess]
    dd = math.sqrt(sum(r * r for r in downside) / len(downside))
    if dd == 0:
        return 0.0
    return statistics.fmean(excess) / dd * math.sqrt(periods_per_year)


def max_drawdown(equity: list[EquityPoint]) -> tuple[float, int]:
    """(max drawdown as a positive fraction, duration in bars)."""
    peak = -math.inf
    max_dd = 0.0
    max_duration = 0
    peak_idx = 0
    for i, point in enumerate(equity):
        if point.equity > peak:
            peak = point.equity
            peak_idx = i
        elif peak > 0:
            dd = (peak - point.equity) / peak
            if dd > max_dd:
                max_dd = dd
                max_duration = i - peak_idx
    return max_dd, max_duration


def calmar_ratio(equity: list[EquityPoint], periods_per_year: float = 252) -> float:
    """CAGR / max drawdown; 0 when drawdown is zero."""
    dd, _ = max_drawdown(equity)
    if dd == 0:
        return 0.0
    return cagr(equity, periods_per_year) / dd


def win_rate(trades: list[Trade]) -> float:
    """Fraction of trades with positive P&L."""
    if not trades:
        return 0.0
    return sum(1 for t in trades if t.pnl > 0) / len(trades)


def profit_factor(trades: list[Trade]) -> float:
    """Gross profit / gross loss; inf when no losing trades."""
    gains = sum(t.pnl for t in trades if t.pnl > 0)
    losses = -sum(t.pnl for t in trades if t.pnl < 0)
    if losses == 0:
        return math.inf if gains > 0 else 0.0
    return gains / losses


def expectancy(trades: list[Trade]) -> float:
    """Mean P&L per trade."""
    if not trades:
        return 0.0
    return statistics.fmean(t.pnl for t in trades)


def summarize(
    equity: list[EquityPoint],
    trades: list[Trade],
    periods_per_year: float = 252,
    risk_free: float = 0.0,
) -> dict[str, float]:
    """All headline metrics in one dict."""
    dd, dd_duration = max_drawdown(equity)
    return {
        "total_return": total_return(equity),
        "cagr": cagr(equity, periods_per_year),
        "annualized_volatility": annualized_volatility(equity, periods_per_year),
        "sharpe_ratio": sharpe_ratio(equity, periods_per_year, risk_free),
        "sortino_ratio": sortino_ratio(equity, periods_per_year, risk_free),
        "max_drawdown": dd,
        "max_drawdown_duration_bars": float(dd_duration),
        "calmar_ratio": calmar_ratio(equity, periods_per_year),
        "num_trades": float(len(trades)),
        "win_rate": win_rate(trades),
        "profit_factor": profit_factor(trades),
        "expectancy": expectancy(trades),
    }
