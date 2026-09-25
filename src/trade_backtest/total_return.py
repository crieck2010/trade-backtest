"""Total return: dividends, splits, and the buy-and-hold benchmark.

Price-return backtests lie quietly. The quietest version of the lie is
ignoring everything that is not price: a strategy that holds a
dividend payer through the ex-date earned that dividend, and a backtest
that never credits it understates the strategy -- while a backtest that
ignores splits misstates every per-share number before the split. This
module makes the basis *declared*, never silent:

* ``AdjustmentBasis`` -- the three declared bases a bar series can rest
  on. There is no fourth, silent option: the engine requires one.
* ``CorporateAction`` / ``Dividend`` / ``Split`` -- plain frozen
  dataclasses describing what happened, on which ex-date (UTC).
* ``AdjustedDataHandler`` -- wraps any ``BarDataHandler`` and
  backward-adjusts pre-split bars so the strategy sees one continuous
  price series. Dividends never touch bars; they flow through the
  portfolio's cash (see ``Portfolio.apply_corporate_action``).
* ``buy_and_hold_curve`` -- the total-return benchmark: buy at the first
  bar's open, hold, reinvest dividends at the ex-date close.
* ``excess_vs_benchmark`` -- strategy vs benchmark on aligned curves.

The maths:

* *Split backward-adjustment*: for a split with ratio ``r`` (new shares
  per old) and ex-date ``d``, every bar dated before ``d`` gets
  ``O/H/L/C / r`` and ``volume * r``. Multiple splits compound
  multiplicatively. Bars on or after ``d`` are untouched.
* *Total return*: ``TR = price return + dividend yield effects`` --
  dividends are credited to cash (or reinvested into new shares at the
  ex-date close, commission-free) instead of vanishing.
* *Short dividends*: a short position *pays* the dividend -- cash is
  debited ``|qty| * amount_per_share`` on the ex-date.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

from .data import BarDataHandler
from .exceptions import BacktestError, DataError
from .models import Bar, EquityPoint, ensure_utc


class AdjustmentBasis:
    """The declared basis of a bar series. Pick one, out loud.

    * ``PRE_ADJUSTED`` -- the data provider already adjusted for splits
      and dividends (verify this for your feed; most vendors do).
    * ``UNADJUSTED_WITH_EVENTS`` -- raw bars plus an explicit
      ``corporate_actions`` list; the engine backward-adjusts splits and
      routes dividends through cash.
    * ``NONE`` -- the asset class has no corporate actions (crypto,
      futures, synthetic/demo bars). A declared basis, never silent.
    """

    PRE_ADJUSTED = "pre_adjusted"
    UNADJUSTED_WITH_EVENTS = "unadjusted_with_events"
    NONE = "none"

    @classmethod
    def all(cls) -> tuple[str, ...]:
        return (cls.PRE_ADJUSTED, cls.UNADJUSTED_WITH_EVENTS, cls.NONE)

    @classmethod
    def validate(cls, value: str | None) -> str:
        if value not in cls.all():
            raise BacktestError(
                "adjustment_basis must be one of "
                f"{list(cls.all())}; got {value!r}. Declare the basis of your "
                "bars -- 'none' for assets without corporate actions."
            )
        return value


@dataclass(frozen=True)
class CorporateAction:
    """Base corporate action: what happened to ``symbol`` on ``ex_date``."""

    symbol: str
    ex_date: date

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol must be a non-empty string")
        object.__setattr__(self, "symbol", symbol)
        ex_date = self.ex_date
        if isinstance(ex_date, datetime):
            ex_date = ensure_utc(ex_date).date()
        if not isinstance(ex_date, date):
            raise TypeError(f"ex_date must be a date, got {type(ex_date).__name__}")
        object.__setattr__(self, "ex_date", ex_date)


@dataclass(frozen=True)
class Dividend(CorporateAction):
    """Cash dividend of ``amount_per_share`` with ex-date ``ex_date``.

    Dividends do not touch bars -- on the ex-date the portfolio credits
    (long) or debits (short) ``|qty| * amount_per_share`` in cash, or
    reinvests into new shares at the ex-date close.
    """

    amount_per_share: float

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.amount_per_share, (int, float)) or not isfinite(
            self.amount_per_share
        ):
            raise ValueError(
                f"amount_per_share must be finite, got {self.amount_per_share!r}"
            )
        if self.amount_per_share < 0:
            raise ValueError(
                f"amount_per_share must be non-negative, got {self.amount_per_share!r}"
            )
        object.__setattr__(self, "amount_per_share", float(self.amount_per_share))


@dataclass(frozen=True)
class Split(CorporateAction):
    """Stock split with ex-date ``ex_date``; ``ratio`` is new shares per
    old share (2.0 for a 2:1 split, 0.2 for a 1:5 reverse split)."""

    ratio: float

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.ratio, (int, float)) or not isfinite(self.ratio):
            raise ValueError(f"ratio must be finite, got {self.ratio!r}")
        if self.ratio <= 0:
            raise ValueError(f"ratio must be positive, got {self.ratio!r}")
        object.__setattr__(self, "ratio", float(self.ratio))


class AdjustedDataHandler(BarDataHandler):
    """Backward-adjust a bar stream for splits.

    Wraps any ``BarDataHandler``. For each symbol, splits are applied in
    ex-date order: a bar dated *before* a split's ex-date has ``O/H/L/C``
    divided by the split ratio and ``volume`` multiplied by it (volume
    scales inversely -- cheaper shares trade more of them). Bars on or
    after the ex-date pass through untouched, so the strategy sees one
    continuous series with no artificial gap at the split.

    ``symbols`` and every other property delegate to the wrapped handler.
    """

    def __init__(self, inner: BarDataHandler, actions: Iterable[CorporateAction]) -> None:
        self._inner = inner
        self.actions = tuple(actions)
        splits: dict[str, list[Split]] = {}
        for action in self.actions:
            if isinstance(action, Split):
                splits.setdefault(action.symbol, []).append(action)
        for symbol_splits in splits.values():
            symbol_splits.sort(key=lambda s: s.ex_date)
        self._splits = splits

    @property
    def symbols(self) -> list[str]:
        return self._inner.symbols

    @property
    def inner(self) -> BarDataHandler:
        return self._inner

    def stream(self):
        for timestamp, bars in self._inner.stream():
            yield timestamp, {s: self._adjust(b) for s, b in bars.items()}

    def _adjust(self, bar: Bar) -> Bar:
        factor = 1.0
        for split in self._splits.get(bar.symbol, ()):
            if bar.timestamp.date() < split.ex_date:
                factor *= split.ratio
        if factor == 1.0:
            return bar
        return Bar(
            symbol=bar.symbol,
            timestamp=bar.timestamp,
            open=bar.open / factor,
            high=bar.high / factor,
            low=bar.low / factor,
            close=bar.close / factor,
            volume=bar.volume * factor,
        )


def buy_and_hold_curve(
    bars: list[Bar],
    initial_cash: float,
    adjustment_basis: str,
    corporate_actions: Iterable[CorporateAction] = (),
    dividend_reinvestment: bool = True,
) -> list[EquityPoint]:
    """Total-return buy-and-hold benchmark over ``bars`` (single symbol).

    Buys ``initial_cash / first_bar.open`` shares at the first bar's open
    and holds. On a dividend ex-date the payout is reinvested into new
    shares at that bar's close -- commission-free, by construction -- or
    held as cash when ``dividend_reinvestment`` is False. Returns the
    equity curve as ``EquityPoint``s, one per bar.
    """
    AdjustmentBasis.validate(adjustment_basis)
    if not bars:
        raise DataError("buy_and_hold_curve needs at least one bar")
    actions = tuple(corporate_actions)
    if actions and adjustment_basis != AdjustmentBasis.UNADJUSTED_WITH_EVENTS:
        raise BacktestError(
            "corporate_actions require adjustment_basis='unadjusted_with_events'; "
            f"got {adjustment_basis!r}"
        )
    if not isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError(f"initial_cash must be positive, got {initial_cash!r}")

    symbol = bars[0].symbol
    ordered = sorted(bars, key=lambda b: b.timestamp)
    dividends: dict[date, float] = {}
    for action in actions:
        if isinstance(action, Dividend):
            if action.symbol != symbol:
                raise DataError(
                    f"buy_and_hold_curve is single-symbol ({symbol}); "
                    f"got action for {action.symbol}"
                )
            dividends[action.ex_date] = dividends.get(action.ex_date, 0.0) + action.amount_per_share

    shares = initial_cash / ordered[0].open
    cash = 0.0
    curve: list[EquityPoint] = []
    for bar in ordered:
        if bar.symbol != symbol:
            raise DataError(
                f"buy_and_hold_curve is single-symbol ({symbol}); got {bar.symbol}"
            )
        payout_per_share = dividends.get(bar.timestamp.date(), 0.0)
        if payout_per_share:
            payout = shares * payout_per_share
            if dividend_reinvestment and bar.close > 0:
                shares += payout / bar.close
            else:
                cash += payout
        curve.append(
            EquityPoint(timestamp=bar.timestamp, equity=cash + shares * bar.close, cash=cash)
        )
    return curve


def excess_vs_benchmark(
    strategy_curve: list[EquityPoint], benchmark_curve: list[EquityPoint]
) -> dict:
    """Strategy vs benchmark on two aligned equity curves.

    Both curves must cover the same timestamps in the same order --
    anything else is a shape error, raised loudly. Returns
    ``strategy_total_return``, ``benchmark_total_return`` and their
    difference, ``excess_return``.
    """
    if len(strategy_curve) != len(benchmark_curve):
        raise BacktestError(
            "curve length mismatch: "
            f"{len(strategy_curve)} strategy points vs {len(benchmark_curve)} benchmark points"
        )
    if not strategy_curve:
        raise BacktestError("empty curves have no excess return")
    for s, b in zip(strategy_curve, benchmark_curve):
        if s.timestamp != b.timestamp:
            raise BacktestError(
                f"curves are not timestamp-aligned: {s.timestamp} vs {b.timestamp}"
            )
    s0, s1 = strategy_curve[0].equity, strategy_curve[-1].equity
    b0, b1 = benchmark_curve[0].equity, benchmark_curve[-1].equity
    strat_ret = s1 / s0 - 1.0 if s0 else 0.0
    bench_ret = b1 / b0 - 1.0 if b0 else 0.0
    return {
        "strategy_total_return": strat_ret,
        "benchmark_total_return": bench_ret,
        "excess_return": strat_ret - bench_ret,
    }
