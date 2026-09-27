"""Event-driven backtesting engine.

Pure-Python, stdlib-only. Strategies consume normalized OHLCV bars --
adapted from any sibling ``trade-data-*`` engine via
:func:`trade_backtest.data.normalize_bar` -- and the engine simulates
fills with slippage and commissions, tracks positions with FIFO
accounting, and reports headline performance metrics.
"""

from .data import BarDataHandler, ListDataHandler, normalize_bar
from .costs import CostModel
from . import adversarial
from .adversarial import (
    AdversarialFailure,
    CASES,
    guard_corporate_actions,
    guard_ffill_gap,
    guard_future_return_leakage,
    guard_timestamp_alignment,
    inject_ffill_gap,
    inject_future_return_leakage,
    inject_timestamp_shift,
    inject_unadjusted_corporate_action,
    make_clean_feed,
    run_adversarial_suite,
    run_case,
    run_guards,
)
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
from .total_return import (
    AdjustedDataHandler,
    AdjustmentBasis,
    CorporateAction,
    Dividend,
    Split,
    buy_and_hold_curve,
    excess_vs_benchmark,
)

__version__ = "0.3.0"

__all__ = [
    "AdjustedDataHandler",
    "AdjustmentBasis",
    "AdversarialFailure",
    "BacktestEngine",
    "BacktestError",
    "BacktestResult",
    "Bar",
    "BarDataHandler",
    "CASES",
    "Commission",
    "CorporateAction",
    "CostModel",
    "DataError",
    "Dividend",
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
    "Split",
    "Strategy",
    "Trade",
    "adversarial",
    "annualized_volatility",
    "buy_and_hold_curve",
    "cagr",
    "calmar_ratio",
    "excess_vs_benchmark",
    "expectancy",
    "guard_corporate_actions",
    "guard_ffill_gap",
    "guard_future_return_leakage",
    "guard_timestamp_alignment",
    "inject_ffill_gap",
    "inject_future_return_leakage",
    "inject_timestamp_shift",
    "inject_unadjusted_corporate_action",
    "make_clean_feed",
    "max_drawdown",
    "normalize_bar",
    "profit_factor",
    "run_adversarial_suite",
    "run_case",
    "run_guards",
    "sharpe_ratio",
    "sortino_ratio",
    "summarize",
    "total_return",
    "win_rate",
]
