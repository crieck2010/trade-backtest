"""Portfolio: positions, cash, signals -> orders, fills -> trades.

The portfolio is the only component that touches money. Flow:

* :meth:`Portfolio.on_signal` turns a strategy :class:`Signal` into an
  :class:`Order` (or ``None``) via the position sizer. Signals express
  *targets* (long / short / flat); the portfolio orders only the delta
  from the current position, so reversals work naturally. Each order is
  marked ``is_entry`` so execution can pick the right slippage knob.
* :meth:`Portfolio.on_fill` applies a :class:`Fill`: cash moves (fill
  price, commission, and the fill's half-spread cost), FIFO lots match,
  closed round-trips append to :attr:`trades`.
* :meth:`Portfolio.mark_to_market` revalues positions;
  :meth:`Portfolio.accrue_borrow_cost` debits borrow on short market
  value (actual/365); :meth:`record` snapshots the equity curve.
* :meth:`Portfolio.apply_corporate_action` handles splits (lot rescale)
  and dividends (cash credit/debit, or reinvestment).

Short selling is permitted; margin calls are *not* modeled here -- that
belongs to ``trade-risk``. Cash may go negative.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from math import isfinite

from .costs import CostModel
from .exceptions import PortfolioError
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
    ensure_utc,
)
from .total_return import CorporateAction, Dividend, Split


class PositionSizer(ABC):
    """Maps a signal to a signed target quantity."""

    @abstractmethod
    def size(self, signal: Signal, price: float, portfolio: "Portfolio") -> float:
        """Signed target quantity: >0 long, <0 short, 0 flat."""
        raise NotImplementedError


class FixedQuantitySizer(PositionSizer):
    """Always target ``quantity`` units (long) or ``-quantity`` (short)."""

    def __init__(self, quantity: float) -> None:
        if not isfinite(quantity) or quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity!r}")
        self.quantity = float(quantity)

    def size(self, signal: Signal, price: float, portfolio: "Portfolio") -> float:
        qty = self.quantity * signal.strength
        if signal.action is SignalAction.LONG:
            return qty
        if signal.action is SignalAction.SHORT:
            return -qty
        return 0.0


class PercentEquitySizer(PositionSizer):
    """Target ``fraction`` of current equity (long) or ``-fraction`` (short).

    ``max_leverage`` caps gross exposure as a multiple of equity.
    """

    def __init__(self, fraction: float, max_leverage: float = 1.0) -> None:
        if not 0 < fraction <= 1:
            raise ValueError(f"fraction must be in (0, 1], got {fraction!r}")
        if not isfinite(max_leverage) or max_leverage <= 0:
            raise ValueError(f"max_leverage must be positive, got {max_leverage!r}")
        self.fraction = fraction
        self.max_leverage = max_leverage

    def size(self, signal: Signal, price: float, portfolio: "Portfolio") -> float:
        if signal.action is SignalAction.EXIT or price <= 0:
            return 0.0
        notional = portfolio.equity * self.fraction * signal.strength
        notional = min(notional, portfolio.equity * self.max_leverage)
        qty = notional / price
        return qty if signal.action is SignalAction.LONG else -qty


class Portfolio:
    """Cash + positions + FIFO trade reconstruction.

    :param cost_model: the cost assumptions; ``None`` selects
        ``CostModel.defaults()`` (costs are default-on).
    :param dividend_reinvestment: when True, dividend payouts on long
        positions buy new shares at the ex-date close, commission-free.
    """

    def __init__(
        self,
        initial_cash: float,
        sizer: PositionSizer,
        *,
        cost_model: CostModel | None = None,
        dividend_reinvestment: bool = True,
    ) -> None:
        if not isfinite(initial_cash) or initial_cash < 0:
            raise ValueError(f"initial_cash must be non-negative, got {initial_cash!r}")
        self.initial_cash = float(initial_cash)
        self.sizer = sizer
        self.cost_model = cost_model if cost_model is not None else CostModel.defaults()
        self.dividend_reinvestment = bool(dividend_reinvestment)
        self.cash = float(initial_cash)
        self.equity = float(initial_cash)
        self.total_borrow_cost = 0.0
        self._lots: dict[str, list[list]] = {}  # symbol -> [[signed_qty, price, timestamp]]
        self._prices: dict[str, float] = {}
        self.trades: list[Trade] = []
        self.equity_curve: list[EquityPoint] = []

    # -- positions ----------------------------------------------------------
    def position(self, symbol: str) -> float:
        return sum(qty for qty, _, _ in self._lots.get(symbol.strip().upper(), []))

    def positions(self) -> dict[str, float]:
        return {s: self.position(s) for s in self._lots if self.position(s) != 0}

    # -- signals -> orders ----------------------------------------------------
    def on_signal(self, signal: Signal, bars: dict[str, Bar]) -> Order | None:
        bar = bars.get(signal.symbol)
        if bar is None:
            raise PortfolioError(f"no bar for signal symbol {signal.symbol}")
        target = self.sizer.size(signal, bar.close, self)
        position = self.position(signal.symbol)
        delta = target - position
        if abs(delta) < 1e-12:
            return None
        return Order(
            symbol=signal.symbol,
            timestamp=signal.timestamp,
            action=OrderAction.BUY if delta > 0 else OrderAction.SELL,
            quantity=abs(delta),
            order_type=OrderType.LIMIT if signal.limit_price else OrderType.MARKET,
            limit_price=signal.limit_price,
            is_entry=_classify_is_entry(position, target),
        )

    # -- fills -> cash/lots/trades ----------------------------------------------
    def on_fill(self, fill: Fill) -> None:
        symbol = fill.symbol
        signed = fill.signed_quantity
        # Fill price already includes slippage; commission and the
        # half-spread cost are charged to cash separately.
        self.cash -= signed * fill.price + fill.commission + fill.spread_cost
        lots = self._lots.setdefault(symbol, [])
        remaining = signed
        # Match against opposite-sign lots, FIFO; commission is split
        # across the closed legs proportionally.
        matches: list[tuple[float, float, datetime]] = []  # (matched_qty, lot_price, lot_time)
        while abs(remaining) > 1e-12 and lots and _sign(lots[0][0]) != _sign(remaining):
            lot_qty, lot_price, lot_time = lots[0]
            matched = _sign(remaining) * min(abs(remaining), abs(lot_qty))
            matches.append((matched, lot_price, lot_time))
            lot_qty += matched
            remaining -= matched
            if abs(lot_qty) < 1e-12:
                lots.pop(0)
            else:
                lots[0][0] = lot_qty
        for matched, lot_price, lot_time in matches:
            pnl = -(fill.price - lot_price) * matched
            leg_commission = fill.commission * abs(matched) / abs(signed) if signed else 0.0
            self.trades.append(
                Trade(
                    symbol=symbol, entry_time=lot_time, exit_time=fill.timestamp,
                    quantity=abs(matched), entry_price=lot_price,
                    exit_price=fill.price, pnl=pnl, commission=leg_commission,
                )
            )
        if abs(remaining) > 1e-12:
            lots.append([remaining, fill.price, fill.timestamp])
        self._prices[symbol] = fill.price

    # -- valuation ----------------------------------------------------------------
    def mark_to_market(self, timestamp: datetime, bars: dict[str, Bar]) -> None:
        for symbol, bar in bars.items():
            self._prices[symbol] = bar.close
        total = self.cash
        for symbol, lots in self._lots.items():
            qty = sum(q for q, _, _ in lots)
            if qty != 0:
                price = self._prices.get(symbol)
                if price is None:
                    raise PortfolioError(f"no price to value {symbol}")
                total += qty * price
        self.equity = total

    def accrue_borrow_cost(
        self, timestamp: datetime, prev_timestamp: datetime | None
    ) -> float:
        """Accrue borrow cost on short market value since ``prev_timestamp``.

        ``short_mv * annual_bps / 10000 * days / 365`` (actual/365 day
        count), debited from cash and accumulated in
        :attr:`total_borrow_cost`. Skipped when there is no previous
        timestamp, no elapsed time, no short exposure, or the cost model
        is disabled / carries a zero borrow rate. Returns the amount.
        """
        if prev_timestamp is None:
            return 0.0
        days = (timestamp - prev_timestamp).total_seconds() / 86400.0
        if days <= 0:
            return 0.0
        model = self.cost_model
        if model.is_disabled or model.borrow_cost_annual_bps == 0:
            return 0.0
        short_mv = 0.0
        for symbol, lots in self._lots.items():
            qty = sum(q for q, _, _ in lots)
            if qty < 0:
                price = self._prices.get(symbol)
                if price is None:
                    raise PortfolioError(f"no price to value {symbol}")
                short_mv += -qty * price
        if short_mv <= 0:
            return 0.0
        cost = short_mv * model.borrow_cost_annual_bps / 10_000.0 * days / 365.0
        self.cash -= cost
        self.total_borrow_cost += cost
        return cost

    # -- corporate actions --------------------------------------------------------
    def apply_corporate_action(self, action: CorporateAction, bar: Bar | None = None) -> str:
        """Apply one corporate action; returns a human-readable description.

        * ``Split``: every open lot becomes ``qty * ratio`` shares at
          ``price / ratio`` -- position value is unchanged and FIFO P&L
          across the split is unaffected.
        * ``Dividend``: long holders receive ``qty * amount_per_share``
          -- reinvested into new shares at the ex-date close
          (commission-free) when ``dividend_reinvestment`` is on, else
          credited to cash. Short holders *pay* the dividend: cash is
          debited. No position: no-op.

        ``bar`` supplies the ex-date close for reinvestment; when it is
        missing (no bar on the ex-date) a dividend falls back to cash
        credit/debit, documented in the returned description.
        """
        symbol = action.symbol
        if isinstance(action, Split):
            lots = self._lots.get(symbol, [])
            for lot in lots:
                lot[0] *= action.ratio
                lot[1] /= action.ratio
            if symbol in self._prices:
                self._prices[symbol] /= action.ratio
            return (
                f"{action.ratio:g}-for-1 split on {symbol} ex {action.ex_date}: "
                f"{len(lots)} open lot(s) rescaled"
            )
        if isinstance(action, Dividend):
            qty = self.position(symbol)
            if qty == 0:
                return (
                    f"dividend ${action.amount_per_share:g}/sh on {symbol} "
                    f"ex {action.ex_date}: no position, no-op"
                )
            payout = abs(qty) * action.amount_per_share
            if qty > 0:
                if self.dividend_reinvestment and bar is not None and bar.close > 0:
                    new_shares = payout / bar.close
                    self._lots.setdefault(symbol, []).append(
                        [new_shares, bar.close, bar.timestamp]
                    )
                    return (
                        f"dividend ${action.amount_per_share:g}/sh on {symbol} "
                        f"ex {action.ex_date}: ${payout:,.2f} reinvested at "
                        f"${bar.close:,.2f} -> +{new_shares:.4f} sh"
                    )
                self.cash += payout
                fallback = " (no bar on ex-date; reinvestment impossible)" if bar is None else ""
                return (
                    f"dividend ${action.amount_per_share:g}/sh on {symbol} "
                    f"ex {action.ex_date}: ${payout:,.2f} credited to cash{fallback}"
                )
            self.cash -= payout
            return (
                f"dividend ${action.amount_per_share:g}/sh on {symbol} "
                f"ex {action.ex_date}: short pays ${payout:,.2f}"
            )
        raise PortfolioError(f"unknown corporate action: {type(action).__name__}")

    def record(self, timestamp: datetime) -> None:
        self.equity_curve.append(
            EquityPoint(timestamp=ensure_utc(timestamp), equity=self.equity, cash=self.cash)
        )


def _sign(value: float) -> int:
    return 1 if value > 0 else -1


def _classify_is_entry(position: float, target: float) -> bool:
    """Decide whether an order opens/increases (entry) or closes/reduces
    (exit) the absolute position.

    * flat -> opening: entry;
    * target flat -> exit;
    * same direction: bigger absolute target is an entry, smaller an exit;
    * reversal (direction flips): the *dominant* leg decides -- if the
      new opening leg is at least as large as the closing leg it is an
      entry, else an exit. Ties classify as entry. This is a documented
      approximation: a reversal really pays the exit knob on the closing
      leg and the entry knob on the opening leg.
    """
    if position == 0:
        return True
    if target == 0:
        return False
    if _sign(target) == _sign(position):
        return abs(target) > abs(position)
    closing_leg = abs(position)
    opening_leg = abs(target)
    return opening_leg >= closing_leg
