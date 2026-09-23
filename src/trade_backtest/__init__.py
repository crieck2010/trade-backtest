"""Event-driven backtesting engine.

Pure-Python, stdlib-only. Strategies consume normalized OHLCV bars --
adapted from any sibling ``trade-data-*`` engine via
:func:`trade_backtest.data.normalize_bar` -- and the engine simulates
fills with slippage and commissions, tracks positions with FIFO
accounting, and reports headline performance metrics.
"""

from .data import BarDataHandler, ListDataHandler, normalize_bar
from .engine import BacktestEngine, BacktestResult
from .exceptions import BacktestError, DataError, ExecutionError, PortfolioError
from .execution import (
    Commission,
    FlatCommission,
    NoCommission,
    PercentCommission,
    PerShareCommission,
    SimulatedExecutionHandler,
)
from .models import (
    Bar,
    EquityPoint,
    Fill,
    Order,
    OrderAction,
    OrderType,
    Signal,
    SignalAction,
    Trade,
)
from .performance import (
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
from .portfolio import FixedQuantitySizer, PercentEquitySizer, Portfolio, PositionSizer
from .strategy import Strategy

__version__ = "0.1.0"

__all__ = [
    "BacktestEngine",
    "BacktestError",
    "BacktestResult",
    "Bar",
    "BarDataHandler",
    "Commission",
    "DataError",
    "EquityPoint",
    "ExecutionError",
    "Fill",
    "FixedQuantitySizer",
    "FlatCommission",
    "ListDataHandler",
    "NoCommission",
    "Order",
    "OrderAction",
    "OrderType",
    "PercentCommission",
    "PercentEquitySizer",
    "PerShareCommission",
    "Portfolio",
    "PortfolioError",
    "PositionSizer",
    "Signal",
    "SignalAction",
    "SimulatedExecutionHandler",
    "Strategy",
    "Trade",
    "annualized_volatility",
    "cagr",
    "calmar_ratio",
    "expectancy",
    "max_drawdown",
    "normalize_bar",
    "profit_factor",
    "sharpe_ratio",
    "sortino_ratio",
    "summarize",
    "total_return",
    "win_rate",
]
