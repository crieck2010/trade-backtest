"""Exception hierarchy for the backtesting engine."""


class BacktestError(Exception):
    """Base class for all engine errors."""


class DataError(BacktestError):
    """Bar data is missing, misaligned, or malformed."""


class PortfolioError(BacktestError):
    """The portfolio rejected a signal or fill (e.g. unknown symbol)."""


class ExecutionError(BacktestError):
    """The execution handler could not fill an order."""
