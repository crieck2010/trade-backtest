# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-09-28

### Fixed
- Negative prints no longer crash the cost path (the round-3
  `CL=F macd_trend` bug: WTI -$37.63 on 2020-04-20 raised
  `ValueError: slippage must be non-negative`). Slippage, half-spread,
  and percent commission are now quoted against notional
  `|price| * quantity` instead of signed price, and the adverse fill
  move is by action sign (BUY pays more, SELL receives less) whatever
  the print's sign. At price 0 all legs are exactly 0. `Fill`
  validation still rejects negative cost legs -- the guard stands, the
  computation is now correct. No future-looking reference is
  introduced, so the adversarial suite's lookahead guards are
  unaffected. See `docs/COSTS_AND_TOTAL_RETURN.md` ("Negative prints")
  for the maths.
- 12 new regression tests (`tests/test_negative_prices.py`): April-2020-
  shaped series (positive -> negative -> positive) through the real
  engine -- no crash, adverse fill direction on both sides of a
  negative print, sane trade accounting, non-negative cost legs on
  every fill. 143 total, all green.

## [0.3.0] - 2026-09-27

### Added
- `adversarial` module: the named adversarial hidden-data-validation suite.
  Four poison injectors plant four leakage classes into synthetic sandbox
  price feeds -- `inject_future_return_leakage` (feature column computed
  from future bars), `inject_timestamp_shift` (feature payload shifted early
  relative to the price columns, the observable form of a row/timestamp
  misalignment), `inject_ffill_gap` (gap forward-filled with the post-gap
  close, a fill value knowable only with lookahead), and
  `inject_unadjusted_corporate_action` (undeclared split/dividend jump
  presented as a genuine price move).
- Four named guards that flag each class through the REAL pipeline (real
  `Bar`s, real `ListDataHandler`, real `BacktestEngine`, real
  `AdjustedDataHandler` -- nothing mocked): `guard_future_return_leakage`
  (feature vs forward-return correlation against a `z/sqrt(n)` band),
  `guard_timestamp_alignment` (carried-vs-recomputed cross-correlation peak
  at the shift lag), `guard_ffill_gap` (flat runs whose value equals the
  first genuine post-run close), `guard_corporate_actions` (split-ratio and
  ex-date-drop signatures on the adjusted stream, cross-checked against
  declared actions).
- `run_adversarial_suite()` (public function) plus a CLI:
  `python -m trade_backtest.adversarial` and the `trade-backtest-adversarial`
  console script (`--list`, `--case NAME`, `--seed`, `--json`, `--quiet`).
  Each injector->guard pair is individually addressable via `CASES` /
  `run_case(name)`; a failure raises `AdversarialFailure` naming the exact
  leakage class. A clean feed must pass with zero flags (clean control).
  Materiality witness: the planted leaky feature traded through the real
  engine prints Sharpe ~4.7 on pure noise.
- `docs/adversarial.md`: detection statistics ("the maths") -- the
  correlation flag band, the lag-of-peak shift signature, the measure-zero
  flat-run argument, and the split-ratio test, including what shift
  magnitude / correlation each detector flags and why.
- 41 new tests (`tests/test_adversarial.py`); 131 total, all green.

### Grandfathering
- Purely additive: no existing guard, threshold, or engine behavior changed;
  no prior validation round is retroactively invalidated. Declaring a real
  `Split`/`Dividend` on the event date clears the corporate-action flag via
  the existing `AdjustedDataHandler` machinery.

## [0.2.0] - 2026-09-24

### The audit that motivated it
- **Costs existed but were partial and opt-in.** `SimulatedExecutionHandler`
  took one `slippage_bps` knob (same for entries and exits), commission
  models per fill, and slippage baked into the fill price with no
  decomposition. Defaults were zero -- a bare handler ran cost-free.
- **Total return was not handled at all.** No dividends, no splits, no
  adjustment basis, no corporate actions, and no benchmark helper
  (no buy-and-hold comparison of any kind).
- **`BacktestResult` carried no assumptions block** -- nothing in the
  report said which costs ran or what the bars were adjusted for.

### Added
- `total_return` module: `AdjustmentBasis` (`"pre_adjusted"`,
  `"unadjusted_with_events"`, `"none"` -- a declared basis, never
  silent), `CorporateAction` / `Dividend` / `Split` (frozen dataclasses,
  UTC ex-dates), `AdjustedDataHandler` (wraps any `BarDataHandler`;
  backward-adjusts O/H/L/C and inversely scales volume for pre-split
  bars; dividends never touch bars), `buy_and_hold_curve`
  (total-return benchmark: buys at the first open, holds, reinvests
  dividends at the ex-date close, commission-free), `excess_vs_benchmark`
  (aligned-curve comparison with total/excess return).
- `costs` module: `CostModel` (frozen dataclass -- commission, separate
  `slippage_entry_bps` / `slippage_exit_bps`, `half_spread_bps`,
  `borrow_cost_annual_bps` at actual/365, `market_impact` hook,
  `disabled_reason`; `defaults()`, `disabled(reason)` with the reason
  *required*, `as_dict()`, non-negative validation) and `fill_cost`
  (per-fill decomposition `{commission, slippage, spread, total}`).
- `Portfolio`: `cost_model` param (`None` -> `CostModel.defaults()`),
  `dividend_reinvestment` flag, `accrue_borrow_cost` (short market value
  x annual bps / 10000 x days / 365, debited from cash, accumulated in
  `total_borrow_cost`), `apply_corporate_action` (splits rescale lots
  qty x ratio / price / ratio; dividends credit longs / debit shorts /
  reinvest at the ex-date close), `on_signal` marks each order
  `is_entry` (dominant-leg rule for reversals, documented).
- `Fill` gains `slippage`, `spread_cost` (both default 0.0 -- old
  constructions keep working) and a `total_cost` property; `Order`
  gains `is_entry: bool = True`.
- `BacktestEngine` requires keyword-only `adjustment_basis` (missing /
  `None` / unknown -> `BacktestError` at construction); accepts
  `adjustment_note` and `corporate_actions` (non-empty actions require
  basis `"unadjusted_with_events"` and wrap the data in
  `AdjustedDataHandler`). Loop order per timestamp is now
  fill -> corporate actions -> mark-to-market -> borrow accrual ->
  strategy -> record. `BacktestResult` gains a **required**
  `assumptions` dict (engine version, basis + note, corporate actions
  applied, dividend reinvestment, full cost model, borrow totals with
  actual/365 day count, `not_modeled` list, periods per year).
- `docs/COSTS_AND_TOTAL_RETURN.md`: rationale ("price-return backtests
  lie quietly") and migration guide for the two API changes.
- 37 new tests (`tests/test_total_return.py`, `tests/test_costs.py`);
  90 total, all green.

### Changed (behavior)
- **Costs are default-on.** A bare `SimulatedExecutionHandler()` now
  applies `CostModel.defaults()` (5 bps slippage each way, 1 bp
  half-spread, $0.005/share commission, 50 bps/yr borrow) instead of
  zero costs. To opt out, pass
  `cost_model=CostModel.disabled("<reason>")` -- the reason is mandatory.
- **Legacy path preserved.** Passing old-style `slippage_bps` and/or
  `commission` explicitly selects legacy mode: a zeroed model plus only
  what was passed (explicit `slippage_bps` sets both knobs). This keeps
  `trade-swing`'s `SimulatedExecutionHandler(slippage_bps=0.0,
  commission=NoCommission())` at exactly zero cost -- its post-hoc cost
  model stays the single cost source, and the sibling exact-agreement
  test still passes.
- Commission is now computed on the raw (pre-slippage) fill price inside
  `fill_cost`, so the decomposition hand-checks exactly. (Previously it
  used the slippage-adjusted price; identical for Flat/PerShare/None,
  negligible for Percent.)
- The half-spread is charged to cash separately, never baked into the
  fill price; slippage stays a price effect (in the price).
- Existing tests: `BacktestEngine(` constructions gained
  `adjustment_basis="none"` (+ note); fill-logic tests that pinned
  zero-cost prices now pass explicit legacy zeros (`slippage_bps=0.0,
  commission=NoCommission()`) since a bare handler is default-on. No
  pinned numeric expectations changed.
- Sibling callers updated: `trade-swing` passes an explicitly disabled
  cost model + `adjustment_basis="none"` (79 tests green, exact
  agreement intact); `trade-strategies` passes
  `adjustment_basis="pre_adjusted"` with a verify-your-feed note
  (54 tests green).

### Honest limitations (still)
- Market impact is a hook, not a model (`None` = not modeled, stated in
  every report). No partial fills, no latency, no margin calls.
- Dividend reinvestment at the ex-date close is an approximation.
- Reversal orders use the dominant-leg slippage knob (documented).
- Borrow accrues on short stock market value only (actual/365); borrow
  on leveraged long cash balances is not modeled.

## [0.1.0] - 2026-09-23

### Added
- Event-driven backtesting engine: `BacktestEngine` streams `(timestamp, {symbol: Bar})`
  pairs through fill -> mark-to-market -> strategy -> signal -> record.
- No lookahead bias by construction: orders signaled on bar *t* fill at bar *t+1*'s open.
- Immutable event models: `Bar`, `Signal`, `Order`, `Fill`, `Trade`, `EquityPoint`.
- `normalize_bar` structural adapter: accepts dicts or any object with
  `timestamp/open/high/low/close/volume` attributes (covers all sibling
  `trade-data-*` bar shapes) without importing them.
- `BarDataHandler` ABC and in-memory `ListDataHandler` (multi-symbol, timestamp-grouped).
- `Strategy` ABC with `on_bar(timestamp, bars) -> [Signal]`.
- `Portfolio`: cash + positions, signal-targets converted to delta orders,
  FIFO lot matching with round-trip `Trade` reconstruction, mark-to-market,
  equity-curve recording. Shorts allowed; no margin model (deferred to trade-risk).
- Position sizers: `FixedQuantitySizer` (strength-scaled), `PercentEquitySizer`
  (fraction of equity, leverage cap).
- `SimulatedExecutionHandler` with adverse slippage in bps and commission
  models (`Flat`, `PerShare`, `Percent`, `None`); limit-order touch logic;
  commission split proportionally across multi-lot closes.
- Pure performance metrics: total return, CAGR, annualized volatility, Sharpe,
  Sortino, max drawdown + duration, Calmar, win rate, profit factor, expectancy,
  plus `summarize()`.
- `examples/sma_crossover.py` runnable SMA-crossover demo on synthetic bars.
- 53 tests, including a hand-computed 10-bar integration test pinning fill
  prices to the bar after each signal.
