"""Bar data handlers and the structural bar adapter.

The engine never imports the sibling data engines. Instead,
:func:`normalize_bar` adapts *any* bar-like object -- a ``dict``, or an
object with ``timestamp/open/high/low/close/volume`` attributes (which is
exactly what ``trade-data-equities``' ``Bar``, ``trade-data-futures``'
``FuturesBar``, and ``trade-data-crypto``'s ``CryptoBar`` expose) -- into
the engine's internal :class:`Bar`. Symbol comes from the object, or from
an explicit override.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from datetime import datetime

from .exceptions import DataError
from .models import Bar, ensure_utc


def _get(source, name: str, default=None):
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def normalize_bar(source, symbol: str | None = None) -> Bar:
    """Adapt a bar-like object or mapping to :class:`Bar`.

    Accepted timestamp forms: ``datetime`` or ISO-8601 string. ``symbol``
    overrides whatever the source carries.
    """
    sym = symbol or _get(source, "symbol") or _get(source, "contract_code")
    if not sym:
        raise DataError("cannot determine symbol for bar; pass symbol= explicitly")
    ts = _get(source, "timestamp")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts is None:
        raise DataError(f"bar for {sym} has no timestamp")
    try:
        return Bar(
            symbol=str(sym),
            timestamp=ensure_utc(ts),
            open=float(_get(source, "open")),
            high=float(_get(source, "high")),
            low=float(_get(source, "low")),
            close=float(_get(source, "close")),
            volume=float(_get(source, "volume", 0.0) or 0.0),
        )
    except (TypeError, ValueError) as exc:
        raise DataError(f"malformed bar for {sym}: {exc}") from exc


class BarDataHandler(ABC):
    """Abstract bar source. ``stream`` yields ``(timestamp, {symbol: Bar})``."""

    @property
    @abstractmethod
    def symbols(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def stream(self) -> Iterator[tuple[datetime, dict[str, Bar]]]:
        """Chronological ``(timestamp, bars)`` pairs; each symbol present at
        most once per timestamp."""
        raise NotImplementedError


class ListDataHandler(BarDataHandler):
    """In-memory handler over an explicit bar list.

    Bars are grouped by timestamp and sorted. Feed it
    :func:`normalize_bar` output from any sibling engine::

        bars = [normalize_bar(b) for b in equity_client.get_bars(...)]
        handler = ListDataHandler(bars)
    """

    def __init__(self, bars: list[Bar]) -> None:
        if not bars:
            raise DataError("ListDataHandler needs at least one bar")
        self._bars = sorted(bars, key=lambda b: (b.timestamp, b.symbol))

    @property
    def symbols(self) -> list[str]:
        return sorted({b.symbol for b in self._bars})

    def stream(self) -> Iterator[tuple[datetime, dict[str, Bar]]]:
        current_ts: datetime | None = None
        bucket: dict[str, Bar] = {}
        for bar in self._bars:
            if current_ts is not None and bar.timestamp != current_ts:
                yield current_ts, bucket
                bucket = {}
            current_ts = bar.timestamp
            if bar.symbol in bucket:
                raise DataError(f"duplicate bar for {bar.symbol} at {bar.timestamp}")
            bucket[bar.symbol] = bar
        if current_ts is not None:
            yield current_ts, bucket
