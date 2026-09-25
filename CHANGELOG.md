# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
