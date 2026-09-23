"""Portfolio: positions, cash, signals -> orders, fills -> trades.

The portfolio is the only component that touches money. Flow:

* :meth:`Portfolio.on_signal` turns a strategy :class:`Signal` into an
  :class:`Order` (or ``None``) via the position sizer. Signals express
  *targets* (long / short / flat); the portfolio orders only the delta
  from the current position, so reversals work naturally.
* :meth:`Portfolio.on_fill` applies a :class:`Fill`: cash moves, FIFO
  lots match, closed round-trips append to :attr:`trades`.
* :meth:`Portfolio.mark_to_market` revalues positions; :meth:`record`
  snapshots the equity curve.

Short selling is permitted; margin and borrow costs are *not* modeled
here -- that belongs to ``trade-risk``. Cash may go negative.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from math import isfinite

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
    """Cash + positions + FIFO trade reconstruction."""

    def __init__(self, initial_cash: float, sizer: PositionSizer) -> None:
        if not isfinite(initial_cash) or initial_cash < 0:
            raise ValueError(f"initial_cash must be non-negative, got {initial_cash!r}")
        self.initial_cash = float(initial_cash)
        self.sizer = sizer
        self.cash = float(initial_cash)
        self.equity = float(initial_cash)
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
        delta = target - self.position(signal.symbol)
        if abs(delta) < 1e-12:
            return None
        return Order(
            symbol=signal.symbol,
            timestamp=signal.timestamp,
            action=OrderAction.BUY if delta > 0 else OrderAction.SELL,
            quantity=abs(delta),
            order_type=OrderType.LIMIT if signal.limit_price else OrderType.MARKET,
            limit_price=signal.limit_price,
        )

    # -- fills -> cash/lots/trades ----------------------------------------------
    def on_fill(self, fill: Fill) -> None:
        symbol = fill.symbol
        signed = fill.signed_quantity
        self.cash -= signed * fill.price + fill.commission
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

    def record(self, timestamp: datetime) -> None:
        self.equity_curve.append(
            EquityPoint(timestamp=ensure_utc(timestamp), equity=self.equity, cash=self.cash)
        )


def _sign(value: float) -> int:
    return 1 if value > 0 else -1
