# Costs and total return in v0.2.0

## Why: price-return backtests lie quietly

Two quiet lies, one release.

**Lie 1: the cost-free fill.** v0.1.0 defaulted to zero costs -- a bare
`SimulatedExecutionHandler()` ran with no slippage, no commission, no
spread, no borrow. Every backtest looked a little better than reality,
and nothing in the report said so. Costs are now **default-on**
(`CostModel.defaults()`: 5 bps slippage each way, 1 bp half-spread,
$0.005/share, 50 bps/yr borrow on shorts), and every `BacktestResult`
carries an `assumptions` block stating exactly which model ran. The
opt-out still exists -- `CostModel.disabled("<reason>")` -- but the
reason is mandatory, because a silent zero-cost run is just the old lie
with better manners.

**Lie 2: the price-only return.** v0.1.0 had no dividends, no splits, no
adjustment basis, and no benchmark. A strategy holding a 4% yielder
through the ex-date earned that 4% in reality and 0% in the backtest;
splits silently rewrote every pre-split per-share number. v0.2.0 makes
the basis **declared** (`adjustment_basis` is required at construction --
missing/`None`/unknown raises `BacktestError`), routes dividends
through cash (longs credited, shorts *pay*, reinvestment at the ex-date
close by default), backward-adjusts splits, and ships a total-return
buy-and-hold benchmark with an excess-return comparison.

## What changed, concretely

| Area | v0.1.0 | v0.2.0 |
|---|---|---|
| Default costs | zero | `CostModel.defaults()` (default-on) |
| Slippage knobs | one (`slippage_bps`, both sides) | `slippage_entry_bps` / `slippage_exit_bps` (default 5/5) |
| Half-spread | did not exist | 1 bp per side, charged to cash, on the fill |
| Borrow | explicitly deferred to trade-risk | 50 bps/yr on short MV, actual/365, in-engine |
| Fill decomposition | price + commission | `Fill.total_cost = commission + slippage + spread` |
| Dividends/splits | not handled | `Dividend`/`Split` actions, ex-date application |
| Adjustment basis | silent | required kwarg; `BacktestError` if missing/unknown |
| Benchmark | none | `buy_and_hold_curve` + `excess_vs_benchmark` |
| Assumptions | none | required `assumptions` dict on every result |

## Migration guide

Two API changes, both deliberate minor-version breaks:

**1. `adjustment_basis` is required.** Every `BacktestEngine(...)`
construction needs it:

```python
# before
engine = BacktestEngine(data, strategy, portfolio, execution)
# after -- pick the true basis of your bars
engine = BacktestEngine(
    data, strategy, portfolio, execution,
    adjustment_basis="pre_adjusted",  # or "unadjusted_with_events", or "none"
    adjustment_note="vendor bars assumed split/dividend-adjusted",
)
```

- `"pre_adjusted"`: your provider already adjusted for splits/dividends
  (verify this -- do not assume it).
- `"unadjusted_with_events"`: raw bars + `corporate_actions=[...]`; the
  engine backward-adjusts splits and routes dividends through cash.
- `"none"`: the asset class has no corporate actions (crypto, futures,
  synthetic/demo bars). A declared basis, never silent.

**2. Costs are default-on.** If your code constructed a bare
`SimulatedExecutionHandler()` and your pinned numbers assumed zero
costs, they will move -- slightly, and honestly. Your options:

```python
from trade_backtest import CostModel, SimulatedExecutionHandler

# a) accept the defaults (recommended for new work)
ex = SimulatedExecutionHandler()

# b) explicit opt-out with a stated reason
ex = SimulatedExecutionHandler(
    cost_model=CostModel.disabled("costs applied post-hoc by my runner"))

# c) legacy: explicit old-style args still mean exactly what they meant
ex = SimulatedExecutionHandler(slippage_bps=0.0, commission=NoCommission())
```

Notes for upgraders:

- `commission` is now computed on the raw (pre-slippage) fill price, so
  `fill_cost` hand-checks exactly. Identical for Flat/PerShare/None;
  negligible for Percent.
- The half-spread is charged to cash on top of price + commission; it is
  *not* in the fill price. Slippage *is* in the price (it is a price
  effect). `Portfolio.on_fill` handles both; direct `Fill(...)`
  constructions default the new fields to 0.0.
- `Order` gained `is_entry: bool = True` (old constructions unaffected).
  The portfolio sets it per order; reversals classify by the dominant
  leg -- documented approximation.
- `BacktestResult` gained required `assumptions: dict`. Construct it in
  tests or read it off `engine.run()`.
- Borrow accrues on short market value only (actual/365), after
  mark-to-market each bar. Long leverage cash debit is not modeled.

## Worked honesty check

Buy 100 shares @ $100, hold through a $2/share dividend ex-date, price
flat (see `tests/test_total_return.py::test_dividend_caught_total_return_not_price_return`):

- price return: **0.0%**
- total return: **2.0%** ($200 reinvested at the $100 close -> 102 shares)

If your backtest reports the first number for this trade, it is lying
about the second. v0.2.0 reports the second, and the assumptions block
says how.

## Known composition caveat: splits through the engine

`AdjustedDataHandler` backward-adjusts pre-split bars (strategy inputs
stay continuous) and `Portfolio.apply_corporate_action` rescales lots
(`qty x ratio`, `price / ratio`) -- each primitive is correct standalone
and hand-tested. Running a split *through the engine* composes both:
fills land at adjusted prices while lots rescale in unadjusted share
terms. Dividend handling through the engine is exact; split handling is
exact at the portfolio level. If you run splits through the full engine
loop, verify the equity path against the unadjusted economics before
trusting it -- this is the one v0.2.0 seam still under scrutiny.
