# Adversarial hidden-data validation

`trade_backtest.adversarial` is the named suite that proves the
no-future-leakage guards work. The engine is lookahead-free *by construction*
(signals fill on the next bar; corporate-action basis is declared, never
silent), but construction is a promise, not a proof. This suite is the proof:
four poison injectors plant four leakage classes into synthetic sandbox
price feeds, and four named guards must flag each one -- through the real
pipeline (real `Bar`s, real `ListDataHandler`, real `BacktestEngine`, real
`AdjustedDataHandler`), never mocks of the guards. A clean feed must pass
with zero flags.

Run it:

```
python -m trade_backtest.adversarial          # full suite
python -m trade_backtest.adversarial --list   # name each case
python -m trade_backtest.adversarial --case ffill_gap
trade-backtest-adversarial --json             # console script
```

Or from Python:

```python
from trade_backtest import run_adversarial_suite, run_case
run_adversarial_suite()          # raises AdversarialFailure naming the class on failure
run_case("timestamp_shift")      # one injector -> guard pair, individually addressable
```

## The four leakage classes

| Case | Injector plants | Guard | Mechanism signature in evidence |
|---|---|---|---|
| `future_return_leakage` | Feature column = sum of next-5 log-returns + noise | `guard_future_return_leakage` | `corr(feature, forward return)` far above the `z/sqrt(n)` band |
| `timestamp_shift` | Feature payload shifted 1 bar early vs the price columns | `guard_timestamp_alignment` | Cross-correlation peak at lag **+1** with c ≈ 1.0 |
| `ffill_gap` | 6-bar gap forward-filled with the *post-gap* close | `guard_ffill_gap` | Flat run whose value equals the first *genuine* post-run close |
| `unadjusted_corporate_action` | Undeclared 2:1 split (or dividend) jump, basis left `"none"` | `guard_corporate_actions` | Single-bar log-return matches `ln(1/ratio)`; survives the real `AdjustedDataHandler` |

## The maths

### 1. Future-return leakage: the correlation band

Under the null hypothesis (the feature is independent of future returns),
the sample Pearson correlation over `n` rows is approximately normal,

```
r ~ N(0, 1/sqrt(n))            (Fisher's approximation)
```

so the guard flags when `|r| >= z / sqrt(n)`. At the default `z = 3` and
`n = 300` the flag band is `|r| >= 0.173`, a per-feature false-positive rate
of ~0.27% two-sided. A planted leak -- the feature *is* the realized forward
return plus a whisper of noise -- correlates at ~0.98, more than 5x the band.
The band tightens as `1/sqrt(n)`: with 1200 rows the same `z = 3` flags
`|r| >= 0.087`, so longer histories catch subtler leaks.

### 2. Timestamp shift: the lag of the cross-correlation peak

The guard recomputes `mom5` causally from the stamped closes and forms

```
c(k) = corr(carried[t], recomputed[t+k])    for k in [-5, +5]
```

A correctly aligned feed is the same series twice, so `c(0) = 1` exactly.
Shifting the feature payload `s` bars early moves the peak to lag `+s` with
`c(s) = 1` -- the peak's *location* is the shift magnitude and direction,
and its *height* (near 1.0, far above any sampling noise, which lives near
`1/sqrt(n)`) proves it is a mechanical shift rather than chance. The guard
flags when the argmax lag is nonzero and the peak clears 0.9.

One honest subtlety, documented in the injector: a *uniform* shift of whole
rows (payload and timestamp together) is unobservable from the feed alone,
because internal alignment is preserved. The observable -- and label-leaking
-- form of a row/timestamp misalignment is the *relative* skew between the
feature columns and the price columns, which is what this injector plants
and the guard detects.

### 3. Gap-fill artifacts: the measure-zero run

Under continuous prices, `P(open == high == low == close)` on any single bar
is ~0, so a maximal run of `>= 3` perfectly flat bars is already a structural
artifact, not chance. The lookahead test is sharper than the artifact test:
a legitimate past-anchored fill repeats the *pre-run* close, while the
planted misdirected fill repeats the close of the first *genuine* (non-flat,
nonzero-volume) bar at or after the run -- a value knowable only with
lookahead. Stale past-anchored runs are reported as observations, not flags;
only the future-smuggling direction fails the suite.

### 4. Corporate-action jumps: the split-ratio test

A `ratio`-for-1 split prints a single-bar log-return of `ln(1/ratio)` going
forward (`-0.6931` for 2:1) or `ln(ratio)` for a reverse split. The guard
scans the *adjusted* bar stream -- bars run through the real
`AdjustedDataHandler`, so declared splits are backward-adjusted away before
the scan -- and flags any bar whose return lands within `tol = 0.02` (in log
space) of a known ratio (`2, 3, 1.5, 5, 10` and inverses) with no declared
action on that date. An ex-dividend drop is subtler (no exact ratio), so the
guard requires the full ex-date signature: a one-bar fall of 0.5%-12% *plus*
a volume print >= 3x the surrounding median *plus* no declared `Dividend` on
the date (dividends never touch bars in the real pipeline -- they flow
through cash -- so the declaration cross-check is the guard for those).

## Materiality

The suite doesn't just flag the leaks; it shows why they matter. The
`future_return_leakage` case trades the planted feature through the real
`BacktestEngine` (features joined to bars by timestamp, exactly as a
research pipeline would): the leaky strategy prints Sharpe ~4.7 on pure
noise. Without the guard, that number would sail through any performance
gate.

## Grandfathering

This suite is purely additive: a new module, new tests, new docs. No
existing guard, threshold, or engine behavior was changed, so no prior
validation round is retroactively invalidated. `run_guards()` appends
findings; it never redefines them.
