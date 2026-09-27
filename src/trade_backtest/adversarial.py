"""Adversarial hidden-data-validation suite: prove the no-future-leakage guards work.

The engine is lookahead-free *by construction* (fills land on the bar after
the signal; corporate-action basis is declared, never silent), and the
corporate-action contract lives in :mod:`trade_backtest.total_return`. What
this module adds is the *adversarial* proof: four named leakage classes, each
with a **poison injector** that plants the leak into a synthetic sandbox
price feed, and a **named guard** that must flag it. The runner feeds every
poisoned feed through the REAL pipeline -- real ``Bar``\\ s, the real
``ListDataHandler``, the real ``BacktestEngine``, the real
``AdjustedDataHandler`` -- never mocks of the guards. A clean feed must pass
with zero flags.

The four leakage classes (each injector->guard pair is individually
addressable via :data:`CASES` and ``run_case(name)``):

* ``future_return_leakage`` -- a feature column computed from *future* bars
  (the classic research-pipeline sin). Guard: Pearson correlation of the
  feature against realized forward returns.
* ``timestamp_shift`` -- feature columns shifted relative to the price
  columns (the observable form of a row/timestamp misalignment; labels
  joined by timestamp then leak). Guard: cross-correlation of the carried
  feature column against the same feature recomputed causally from the
  stamped closes; a shifted copy peaks at a nonzero lag.
* ``ffill_gap`` -- a gap forward-filled with the *future* close (a
  misdirected fill that smuggles future information). Guard: maximal runs of
  identical closes; a run whose flat value equals the *post-run* close could
  only be known with lookahead.
* ``unadjusted_corporate_action`` -- an undeclared split/dividend jump
  presented as a genuine price move. Guard: split-ratio and ex-date-drop
  signatures on the *adjusted* bar stream (via the real
  ``AdjustedDataHandler``), cross-checked against declared actions.

Plain-data contracts throughout: feeds are lists of plain dicts, findings
are plain dicts. Stdlib only; no network, no credentials, no execution or
broker code. Research only.

Run it::

    python -m trade_backtest.adversarial          # full suite
    python -m trade_backtest.adversarial --list   # name each case
    python -m trade_backtest.adversarial --case ffill_gap
    trade-backtest-adversarial --json            # console script, JSON report
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone

from .data import ListDataHandler, normalize_bar
from .engine import BacktestEngine
from .models import Signal, SignalAction
from .portfolio import FixedQuantitySizer, Portfolio
from .strategy import Strategy
from .total_return import AdjustedDataHandler, Dividend, Split

__all__ = [
    "AdversarialFailure",
    "CASES",
    "SPLIT_RATIOS",
    "guard_corporate_actions",
    "guard_ffill_gap",
    "guard_future_return_leakage",
    "guard_timestamp_alignment",
    "inject_ffill_gap",
    "inject_future_return_leakage",
    "inject_timestamp_shift",
    "inject_unadjusted_corporate_action",
    "make_clean_feed",
    "run_adversarial_suite",
    "run_case",
    "run_guards",
    "main",
]


# ---------------------------------------------------------------------------
# exceptions
# ---------------------------------------------------------------------------

class AdversarialFailure(AssertionError):
    """Raised when a guard fails to flag its planted leak. The message always
    names the exact leakage class (the :data:`CASES` key)."""


# ---------------------------------------------------------------------------
# small stdlib statistics
# ---------------------------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation; None when either series is degenerate."""
    n = len(xs)
    if n != len(ys) or n < 3:
        return None
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


# ---------------------------------------------------------------------------
# sandbox feeds (plain data in, plain data out)
# ---------------------------------------------------------------------------

def _log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i + 1] / closes[i]) for i in range(len(closes) - 1)]


def make_clean_feed(n: int = 300, seed: int = 7, symbol: str = "ADV") -> list[dict]:
    """A leakage-free sandbox feed: geometric random walk, pure-noise features.

    Rows are plain dicts: ``timestamp/open/high/low/close/volume`` plus a
    ``features`` dict. ``mom5`` is the causal 5-bar mean log-return (``None``
    until five bars exist); ``noise_a``/``noise_b`` are i.i.d. Gaussian and
    independent of future returns by construction.
    """
    rng = random.Random(seed)
    t0 = datetime(2024, 1, 2, tzinfo=timezone.utc)
    # Close path first: geometric random walk.
    closes = []
    px = 100.0
    for _ in range(n):
        px = px * math.exp(rng.gauss(0.0005, 0.01))
        closes.append(px)
    # Observable bar returns: rets[i] is the log-return of bar i+1, i.e. the
    # return fully determined by closes[i] and closes[i+1]. mom5[i] is the
    # mean of the five returns ending at bar i -- causal *and* exactly
    # recomputable from the stamped close series, which is what the
    # timestamp-alignment guard relies on.
    rets = [math.log(closes[i + 1] / closes[i]) for i in range(n - 1)]
    rows: list[dict] = []
    for i in range(n):
        prev = closes[i - 1] if i > 0 else closes[0] / math.exp(rets[0])
        close = closes[i]
        hi = max(prev, close) * (1.0 + abs(rng.gauss(0, 0.002)))
        lo = min(prev, close) * (1.0 - abs(rng.gauss(0, 0.002)))
        mom5 = sum(rets[i - 5:i]) / 5.0 if i >= 5 else None
        rows.append({
            "symbol": symbol,
            "timestamp": t0 + timedelta(days=i),
            "open": prev,
            "high": hi,
            "low": lo,
            "close": close,
            "volume": 1_000_000.0 * math.exp(rng.gauss(0, 0.2)),
            "features": {
                "mom5": mom5,
                "noise_a": rng.gauss(0, 1),
                "noise_b": rng.gauss(0, 1),
            },
        })
    return rows


def _copy_row(row: dict) -> dict:
    return {**row, "features": dict(row["features"])}


# ---------------------------------------------------------------------------
# injectors: deliberately plant each leak
# ---------------------------------------------------------------------------

def inject_future_return_leakage(feed: list[dict], feature: str = "future_leak",
                                horizon: int = 5, noise: float = 0.005,
                                seed: int | None = None) -> list[dict]:
    """Plant future-return leakage: ``feature[t]`` = sum of the *next*
    ``horizon`` log-returns (i.e. computed from bars t+1..t+horizon) plus a
    whisper of noise. The trailing ``horizon`` rows get ``None`` -- there is
    no future left to peek at."""
    rng = random.Random(seed)
    closes = [r["close"] for r in feed]
    out = [_copy_row(r) for r in feed]
    n = len(out)
    for i in range(n):
        if i + horizon < n:
            future_sum = math.log(closes[i + horizon] / closes[i])
            out[i]["features"][feature] = future_sum + rng.gauss(0, noise)
        else:
            out[i]["features"][feature] = None
    return out


def inject_timestamp_shift(feed: list[dict], shift_bars: int = 1) -> list[dict]:
    """Plant a timestamp/feature misalignment: each row keeps its timestamp
    and OHLCV, but carries the *feature payload* of ``shift_bars`` later
    rows. Labels joined by timestamp then pair the price action of bar ``t``
    with features that have already seen through bar ``t + shift_bars`` --
    the labels leak.

    Note on observability: a *uniform* shift of whole rows (payload and
    timestamp together) is unobservable from the feed alone -- internal
    alignment is preserved. The observable, label-leaking form of a
    row/timestamp misalignment is exactly this relative skew between the
    feature columns and the price columns, which is what the guard detects
    (cross-correlation peak at lag ``+shift_bars``)."""
    if shift_bars < 1:
        raise ValueError(f"shift_bars must be >= 1, got {shift_bars}")
    n = len(feed)
    if shift_bars >= n:
        raise ValueError("shift_bars must be smaller than the feed length")
    out = [_copy_row(r) for r in feed]
    for i in range(n):
        donor = min(i + shift_bars, n - 1)
        out[i]["features"] = dict(feed[donor]["features"])
    return out


def inject_ffill_gap(feed: list[dict], start_frac: float = 0.4,
                     gap_bars: int = 6) -> list[dict]:
    """Plant a future-smuggling gap fill: ``gap_bars`` rows are flattened to
    the *first post-gap* close with zero volume -- the classic misdirected
    forward fill, where the fill value could only be known with lookahead. A
    legitimate past-anchored fill would repeat the *pre-gap* close; this one
    repeats the future."""
    if not 0.0 < start_frac < 1.0:
        raise ValueError(f"start_frac must be in (0, 1), got {start_frac}")
    s = int(len(feed) * start_frac)
    e = min(s + gap_bars, len(feed) - 1)
    future_close = feed[e]["close"]
    out = [_copy_row(r) for r in feed]
    for i in range(s, e):
        out[i]["open"] = out[i]["high"] = out[i]["low"] = out[i]["close"] = future_close
        out[i]["volume"] = 0.0
    return out


def inject_unadjusted_corporate_action(feed: list[dict], kind: str = "split",
                                      ratio: float = 2.0, at_frac: float = 0.5,
                                      dividend_yield: float = 0.03,
                                      volume_spike: float = 5.0) -> list[dict]:
    """Plant a mislabeled corporate-action jump: an undeclared split or
    dividend discontinuity presented as a genuine price move (no ``Split`` /
    ``Dividend`` declared, basis left as ``"none"``).

    * ``kind="split"``: pre-event bars divided by ``ratio`` (the raw
      unadjusted presentation), so bar ``m`` prints a single-bar log-return
      of ``ln(1/ratio)``.
    * ``kind="dividend"``: every bar from ``m`` on is marked down by
      ``dividend_yield``, with a ``volume_spike`` x volume print on the
      ex-date bar -- the ex-date signature, minus the declaration."""
    if kind not in ("split", "dividend"):
        raise ValueError(f"kind must be 'split' or 'dividend', got {kind!r}")
    m = int(len(feed) * at_frac)
    out = [_copy_row(r) for r in feed]
    if kind == "split":
        if ratio <= 0:
            raise ValueError(f"ratio must be positive, got {ratio}")
        for i in range(m):
            for k in ("open", "high", "low", "close"):
                out[i][k] = out[i][k] / ratio
            out[i]["volume"] = out[i]["volume"] * ratio
    else:
        if not 0.0 < dividend_yield < 1.0:
            raise ValueError(f"dividend_yield must be in (0, 1), got {dividend_yield}")
        for i in range(m, len(out)):
            for k in ("open", "high", "low", "close"):
                out[i][k] = out[i][k] * (1.0 - dividend_yield)
        out[m]["volume"] = out[m]["volume"] * volume_spike
    return out


# ---------------------------------------------------------------------------
# guards: named, real detection over real feed data
# ---------------------------------------------------------------------------

def _forward_returns(closes: list[float], horizon: int) -> list[float | None]:
    fwd: list[float | None] = []
    n = len(closes)
    for i in range(n):
        fwd.append(math.log(closes[i + horizon] / closes[i]) if i + horizon < n else None)
    return fwd


def _finding(leakage_class: str, guard: str, flagged: bool,
            evidence: dict) -> dict:
    return {
        "leakage_class": leakage_class,
        "guard": guard,
        "flagged": bool(flagged),
        "evidence": evidence,
    }


def guard_future_return_leakage(feed: list[dict], feature: str,
                               horizon: int = 5, z: float = 3.0) -> dict:
    """Flag a feature column that knows the future.

    Maths: under the null (feature independent of future returns) the sample
    Pearson correlation over ``n`` rows is approximately ``N(0, 1/sqrt(n))``
    (Fisher). The guard flags when ``|r| >= z / sqrt(n)`` -- at ``z = 3`` the
    per-feature false-positive rate is ~0.27%. A planted leak (feature = the
    realized forward return plus a whisper of noise) correlates near 1.0,
    orders of magnitude above the band.
    """
    closes = [r["close"] for r in feed]
    fwd = _forward_returns(closes, horizon)
    pairs = [(r["features"].get(feature), f)
             for r, f in zip(feed, fwd)
             if isinstance(r["features"].get(feature), (int, float))
             and f is not None and math.isfinite(r["features"][feature])]
    n = len(pairs)
    r = _pearson([p[0] for p in pairs], [p[1] for p in pairs]) if pairs else None
    threshold = z / math.sqrt(n) if n else float("inf")
    flagged = r is not None and abs(r) >= threshold
    return _finding(
        "future_return_leakage", "guard_future_return_leakage", flagged,
        {"feature": feature, "horizon_bars": horizon, "n": n,
         "correlation": r, "threshold": threshold, "z": z},
    )


def _recompute_mom5(closes: list[float]) -> list[float | None]:
    """Causal 5-bar mean return, recomputable from stamped closes alone.

    Mirrors :func:`make_clean_feed` exactly: ``mom5[i]`` is the mean of the
    five bar-returns ending at bar ``i`` (``None`` before five bars exist),
    where each bar-return comes from two adjacent closes."""
    rets = _log_returns(closes)
    out: list[float | None] = [None] * len(closes)
    for i in range(5, len(closes)):
        out[i] = sum(rets[i - 5:i]) / 5.0
    return out


def guard_timestamp_alignment(feed: list[dict], feature: str = "mom5",
                              max_lag: int = 5, min_peak: float = 0.9) -> dict:
    """Flag rows whose payloads are shifted relative to their timestamps.

    Maths: the guard recomputes the ``mom5`` feature *causally* from the
    stamped closes and cross-correlates the carried column against it:
    ``c(k) = corr(carried[t], recomputed[t+k])``. A correctly aligned feed is
    the same series twice, so ``c(0) = 1`` exactly. Shifting every payload by
    ``s`` bars moves the peak to lag ``s`` with ``c(s) = 1`` -- the peak's
    *location* is the shift, and its *height* (near 1.0, far above any
    sampling noise) is the proof it is a mechanical shift rather than chance.
    The guard flags when the argmax lag is nonzero and the peak clears
    ``min_peak``."""
    closes = [r["close"] for r in feed]
    recomputed = _recompute_mom5(closes)
    carried = [r["features"].get(feature) for r in feed]
    idx = [i for i in range(len(feed))
           if isinstance(carried[i], (int, float)) and recomputed[i] is not None]
    lags: dict[int, float | None] = {}
    for k in range(-max_lag, max_lag + 1):
        xs, ys = [], []
        for i in idx:
            j = i + k
            if 0 <= j < len(feed) and recomputed[j] is not None:
                xs.append(carried[i])  # type: ignore[arg-type]
                ys.append(recomputed[j])
        lags[k] = _pearson(xs, ys)
    scored = {k: v for k, v in lags.items() if v is not None}
    peak_lag = max(scored, key=lambda k: abs(scored[k])) if scored else 0
    peak = scored.get(peak_lag)
    flagged = peak_lag != 0 and peak is not None and abs(peak) >= min_peak
    return _finding(
        "timestamp_shift", "guard_timestamp_alignment", flagged,
        {"feature": feature, "max_lag": max_lag, "peak_lag_bars": peak_lag,
         "peak_correlation": peak,
         "lag_correlations": {str(k): v for k, v in lags.items()}},
    )


def guard_ffill_gap(feed: list[dict], min_run: int = 3) -> dict:
    """Flag gap fills that smuggle future information.

    Maths: under continuous prices the probability of a bar printing
    *perfectly* flat OHLC (``open == high == low == close``) is ~0, so a
    maximal run of ``>= min_run`` perfectly flat bars is already a structural
    artifact, not chance. The lookahead test is sharper: a legitimate
    past-anchored fill repeats the *pre-run* close, while this injector's
    misdirected fill repeats the close of the first *genuine* (non-flat,
    nonzero-volume) bar at or after the run -- a value knowable only with
    lookahead. The guard flags a flat run whose value equals that future
    genuine close but not the pre-run close (stale past-anchored runs are
    reported as observations, not flags)."""
    n = len(feed)

    def _is_flat(r: dict) -> bool:
        return r["open"] == r["high"] == r["low"] == r["close"]

    runs = []
    i = 0
    while i < n:
        if not _is_flat(feed[i]):
            i += 1
            continue
        j = i
        while (j + 1 < n and _is_flat(feed[j + 1])
               and feed[j + 1]["close"] == feed[i]["close"]):
            j += 1
        length = j - i + 1
        if length >= min_run:
            flat = feed[i]["close"]
            prev_close = feed[i - 1]["close"] if i > 0 else None
            # First genuine (non-flat or nonzero-volume) bar at/after the run:
            # the only place a future-anchored fill value can come from.
            k = j + 1
            while k < n and _is_flat(feed[k]) and feed[k]["volume"] == 0:
                k += 1
            next_genuine = feed[k]["close"] if k < n else None
            runs.append({
                "start": i,
                "length": length,
                "flat_close": flat,
                "equals_pre_run_close": prev_close == flat,
                "equals_post_genuine_close": next_genuine == flat,
                "smuggles_future": next_genuine == flat and prev_close != flat,
            })
        i = j + 1
    flagged = any(r["smuggles_future"] for r in runs)
    return _finding(
        "ffill_gap", "guard_ffill_gap", flagged,
        {"min_run": min_run, "flat_runs": runs,
         "n_future_smuggling_runs": sum(1 for r in runs if r["smuggles_future"])},
    )


#: Split ratios the guard recognizes (new shares per old share and inverses).
SPLIT_RATIOS = (2.0, 3.0, 1.5, 5.0, 10.0, 0.5, 1 / 3, 2 / 3, 0.2, 0.1)


def _declared_ex_dates(actions) -> set:
    return {a.ex_date for a in actions
            if isinstance(a, (Split, Dividend))}


def guard_corporate_actions(feed: list[dict], actions: tuple = (),
                           basis: str = "none", tol: float = 0.02,
                           dividend_drop_range: tuple[float, float] = (0.005, 0.12),
                           volume_spike: float = 3.0) -> dict:
    """Flag unadjusted split/dividend jumps presented as genuine price moves.

    The guard runs the feed's bars through the REAL ``AdjustedDataHandler``
    (the same class the engine uses): declared splits are backward-adjusted
    away, so any discontinuity that *survives* adjustment -- or that matches a
    corporate-action signature with no declared action on its date -- is
    unadjusted data wearing a genuine-move costume.

    Maths: a ``ratio``-for-1 split prints a single-bar log-return of
    ``ln(1/ratio)`` (e.g. -0.6931 for 2:1); the guard flags bars whose return
    lands within ``tol`` (in log space) of a known ratio with no declared
    action on that date. An ex-dividend drop prints a one-bar fall of
    ``dividend_drop_range`` with an ex-date volume spike (>= ``volume_spike``
    x the surrounding median) and no declared ``Dividend`` -- dividends never
    touch bars in the real pipeline (they flow through cash), so the
    declaration cross-check is the guard for those.
    """
    rows = sorted(feed, key=lambda r: r["timestamp"])
    bars = [normalize_bar(r) for r in rows]
    handler = AdjustedDataHandler(ListDataHandler(bars), list(actions))
    adj_closes: list[float] = []
    adj_vols: list[float] = []
    adj_ts: list[datetime] = []
    for ts, bucket in handler.stream():
        bar = bucket[rows[0]["symbol"].strip().upper()]
        adj_ts.append(ts)
        adj_closes.append(bar.close)
        adj_vols.append(bar.volume)

    declared = _declared_ex_dates(actions)
    split_hits, div_hits = [], []
    for i in range(1, len(adj_closes)):
        r = math.log(adj_closes[i] / adj_closes[i - 1])
        d = adj_ts[i].date()
        if d in declared:
            continue  # the AdjustmentBasis contract honored it: not a leak
        # One hit per bar: the closest (ratio, direction) match wins, so a
        # single 2:1 event is not reported twice (2.0 forward, 0.5 inverse).
        best: tuple[float, float, float] | None = None
        for ratio in SPLIT_RATIOS:
            for target in (math.log(1.0 / ratio), math.log(ratio)):
                err = abs(r - target)
                if best is None or err < best[0]:
                    best = (err, ratio, target)
        if best is not None and best[0] <= tol:
            split_hits.append({
                "index": i, "date": adj_ts[i].isoformat(),
                "log_return": r, "implied_ratio": best[1],
                "matched_log_return": best[2],
            })
        lo, hi = dividend_drop_range
        drop = -r
        if lo <= drop <= hi:
            window = adj_vols[max(0, i - 10):i + 11]
            med = _median(window) if window else 0.0
            if med > 0 and adj_vols[i] >= volume_spike * med:
                div_hits.append({
                    "index": i, "date": adj_ts[i].isoformat(),
                    "drop": drop, "volume": adj_vols[i],
                    "median_volume": med,
                })
    flagged = bool(split_hits or div_hits)
    return _finding(
        "unadjusted_corporate_action", "guard_corporate_actions", flagged,
        {"basis": basis, "n_declared_actions": len(actions),
         "split_candidates": split_hits, "dividend_candidates": div_hits},
    )


def run_guards(feed: list[dict], actions: tuple = (),
               basis: str = "none") -> list[dict]:
    """Run every named guard over one feed. Additive: new guards append new
    findings; existing guards and their thresholds never change, so prior
    validation rounds are never retroactively invalidated."""
    leak_features = sorted({k for r in feed for k in r["features"]
                            if k == "future_leak"})
    findings = [guard_future_return_leakage(feed, feature=f) for f in leak_features]
    findings.append(guard_timestamp_alignment(feed))
    findings.append(guard_ffill_gap(feed))
    findings.append(guard_corporate_actions(feed, actions=actions, basis=basis))
    return findings


# ---------------------------------------------------------------------------
# the real pipeline, end to end
# ---------------------------------------------------------------------------

class _BuyAndHoldOnce(Strategy):
    """Trivial strategy for pipeline integration: long on the first bar."""

    def __init__(self, symbol: str):
        super().__init__([symbol])
        self._done = False

    def on_bar(self, timestamp, bars) -> list[Signal]:
        if self._done:
            return []
        self._done = True
        return [Signal(self.symbols[0], timestamp, SignalAction.LONG)]


class _LeakySignalStrategy(Strategy):
    """Trades the planted ``future_leak`` feature through the real engine.

    The feature map is joined by timestamp, exactly as a research pipeline
    would join a feature store to bars. Materiality witness: if the feature
    truly sees the future, this strategy's Sharpe is absurd -- which is
    precisely why the guard must exist."""

    def __init__(self, symbol: str, feature_by_ts: dict):
        super().__init__([symbol])
        self._f = feature_by_ts

    def on_bar(self, timestamp, bars) -> list[Signal]:
        f = self._f.get(timestamp)
        if not isinstance(f, (int, float)):
            return []
        action = SignalAction.LONG if f > 0 else SignalAction.EXIT
        return [Signal(self.symbols[0], timestamp, action)]


def _engine_run(feed: list[dict], strategy: Strategy,
                note: str, corporate_actions: list | None = None) -> dict:
    """Push a sandbox feed through the real engine. Returns headline stats."""
    bars = [normalize_bar(r) for r in sorted(feed, key=lambda r: r["timestamp"])]
    engine = BacktestEngine(
        ListDataHandler(bars),
        strategy,
        Portfolio(100_000.0, FixedQuantitySizer(100)),
        adjustment_basis="none",
        adjustment_note=note,
        corporate_actions=corporate_actions or [],
    )
    result = engine.run()
    return {
        "bars": len(bars),
        "trades": result.num_trades,
        "final_equity": result.final_equity,
        "sharpe_ratio": result.metrics.get("sharpe_ratio"),
        "total_return": result.metrics.get("total_return"),
        "assumptions": result.assumptions,
    }


# ---------------------------------------------------------------------------
# case registry + runner
# ---------------------------------------------------------------------------

CASES: dict[str, dict] = {
    "future_return_leakage": {
        "description": "Feature column computed from future bars (injector: "
                       "feature[t] = sum of next-5 log-returns + noise).",
        "injector": inject_future_return_leakage,
        "inject_kwargs": {"feature": "future_leak", "horizon": 5},
        "guard": guard_future_return_leakage,
        "guard_kwargs": {"feature": "future_leak", "horizon": 5},
    },
    "timestamp_shift": {
        "description": "Feature payload shifted 1 bar early relative to the "
                       "price columns: labels joined by timestamp leak.",
        "injector": inject_timestamp_shift,
        "inject_kwargs": {"shift_bars": 1},
        "guard": guard_timestamp_alignment,
        "guard_kwargs": {"feature": "mom5", "max_lag": 5},
    },
    "ffill_gap": {
        "description": "6-bar gap forward-filled with the post-gap close -- "
                       "a fill value knowable only with lookahead.",
        "injector": inject_ffill_gap,
        "inject_kwargs": {"start_frac": 0.4, "gap_bars": 6},
        "guard": guard_ffill_gap,
        "guard_kwargs": {"min_run": 3},
    },
    "unadjusted_corporate_action": {
        "description": "Undeclared 2:1 split jump presented as a genuine "
                       "price move (basis left 'none').",
        "injector": inject_unadjusted_corporate_action,
        "inject_kwargs": {"kind": "split", "ratio": 2.0, "at_frac": 0.5},
        "guard": guard_corporate_actions,
        "guard_kwargs": {"actions": (), "basis": "none"},
    },
}


def run_case(name: str, seed: int = 7) -> dict:
    """Run one injector->guard pair through the real pipeline.

    Builds a clean sandbox feed, poisons it, runs the named guard, asserts
    the guard flags the planted leak, then pushes the poisoned feed through
    the real ``BacktestEngine`` for end-to-end integration. Returns the case
    report. Raises :class:`AdversarialFailure` naming the leakage class on
    any failure."""
    if name not in CASES:
        raise AdversarialFailure(
            f"ADVERSARIAL FAILURE [{name}]: unknown leakage class; "
            f"expected one of {sorted(CASES)}")
    case = CASES[name]
    clean = make_clean_feed(seed=seed)
    # The suite seed flows into any injector that accepts one, so the whole
    # run is deterministic; injectors without stochastic parts take none.
    inject_kwargs = dict(case["inject_kwargs"])
    if "seed" in inspect.signature(case["injector"]).parameters:
        inject_kwargs["seed"] = seed
    poisoned = case["injector"](clean, **inject_kwargs)
    finding = case["guard"](poisoned, **case["guard_kwargs"])
    if not finding["flagged"]:
        raise AdversarialFailure(
            f"ADVERSARIAL FAILURE [{name}]: guard "
            f"'{finding['guard']}' did not flag the planted leak. "
            f"Evidence: {finding['evidence']}")

    # The poisoned feed goes through the REAL engine, not a mock of it.
    symbol = poisoned[0]["symbol"]
    if name == "future_return_leakage":
        feat = case["inject_kwargs"]["feature"]
        fmap = {r["timestamp"]: r["features"].get(feat) for r in poisoned}
        strategy: Strategy = _LeakySignalStrategy(symbol, fmap)
    else:
        strategy = _BuyAndHoldOnce(symbol)
    engine_stats = _engine_run(
        poisoned, strategy,
        note=f"adversarial sandbox: leakage class '{name}'",
        corporate_actions=[] if name != "unadjusted_corporate_action" else [],
    )
    return {
        "case": name,
        "description": case["description"],
        "flagged": True,
        "finding": finding,
        "engine": engine_stats,
    }


def run_adversarial_suite(cases: list[str] | None = None,
                          seed: int = 7) -> dict:
    """Run the full adversarial suite: every injector->guard pair, plus a
    clean-feed control that must pass with zero flags.

    Returns the report dict; raises :class:`AdversarialFailure` (naming the
    exact leakage class) on the first failure. Grandfathering: this suite is
    purely additive -- it changes no existing guard, threshold, or engine
    behavior, so no prior validation round is retroactively invalidated."""
    names = list(CASES) if cases is None else list(cases)
    case_reports = {}
    for name in names:
        case_reports[name] = run_case(name, seed=seed)

    # Clean-feed control: the real guards must stay silent.
    clean = make_clean_feed(seed=seed)
    control = run_guards(clean)
    noisy = [f for f in control if f["flagged"]]
    if noisy:
        raise AdversarialFailure(
            "ADVERSARIAL FAILURE [clean_control]: guards flagged a clean "
            f"feed: {[f['leakage_class'] for f in noisy]}")
    return {
        "suite": "adversarial_hidden_data_validation",
        "seed": seed,
        "passed": True,
        "cases": case_reports,
        "clean_control": {"flagged": [], "n_findings": len(control)},
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m trade_backtest.adversarial`` or the
    ``trade-backtest-adversarial`` console script."""
    parser = argparse.ArgumentParser(
        prog="trade-backtest-adversarial",
        description="Adversarial hidden-data-validation suite: plant four "
                    "leakage classes into sandbox feeds and prove the named "
                    "guards flag each one through the real pipeline.")
    parser.add_argument("--list", action="store_true",
                        help="list the individually addressable cases")
    parser.add_argument("--case", metavar="NAME",
                        help="run a single case (see --list)")
    parser.add_argument("--seed", type=int, default=7,
                        help="RNG seed for the sandbox feeds (default: 7)")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON")
    parser.add_argument("--quiet", action="store_true",
                        help="print only PASS/FAIL")
    args = parser.parse_args(argv)

    if args.list:
        for name, case in CASES.items():
            print(f"{name}: {case['description']}")
        return 0

    try:
        if args.case:
            report = {"case_report": run_case(args.case, seed=args.seed),
                      "passed": True}
        else:
            report = run_adversarial_suite(seed=args.seed)
    except AdversarialFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    elif not args.quiet:
        if "case_report" in report:
            cr = report["case_report"]
            print(f"PASS [{cr['case']}]: guard '{cr['finding']['guard']}' "
                  f"flagged the planted leak.")
        else:
            print(f"PASS: adversarial suite ({len(report['cases'])} cases, "
                  f"clean control silent).")
            for name, cr in report["cases"].items():
                ev = cr["finding"]["evidence"]
                print(f"  PASS [{name}] via {cr['finding']['guard']}")
                _print_evidence(name, ev)
    else:
        print("PASS")
    return 0


def _print_evidence(name: str, ev: dict) -> None:
    if name == "future_return_leakage":
        print(f"      corr(feature, fwd return) = {ev['correlation']:.3f} "
              f"(flag band |r| >= {ev['threshold']:.3f})")
    elif name == "timestamp_shift":
        print(f"      cross-correlation peak at lag {ev['peak_lag_bars']} "
              f"bars (c = {ev['peak_correlation']:.3f})")
    elif name == "ffill_gap":
        runs = ev["flat_runs"]
        print(f"      {ev['n_future_smuggling_runs']} future-smuggling flat "
              f"run(s); longest run = {max(r['length'] for r in runs)} bars")
    elif name == "unadjusted_corporate_action":
        for h in ev["split_candidates"]:
            print(f"      split-like bar {h['date']}: log-return "
                  f"{h['log_return']:.4f} ~= {h['matched_log_return']:.4f} "
                  f"(implied ratio {h['implied_ratio']})")
        for h in ev["dividend_candidates"]:
            print(f"      dividend-like bar {h['date']}: drop {h['drop']:.4f} "
                  f"with volume {h['volume']:.0f} "
                  f"(surrounding median {h['median_volume']:.0f})")


if __name__ == "__main__":
    raise SystemExit(main())
