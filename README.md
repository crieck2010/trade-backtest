# trade-backtest

An event-driven backtesting engine for the [trade-suite](https://github.com/crieck2010/trade-suite) algorithmic trading system. Pure Python, stdlib-only: no NumPy, no pandas, no data-vendor lock-in.

Strategies consume normalized OHLCV bars, emit signals, and the engine simulates execution with slippage and commissions, tracks cash and positions with FIFO accounting, and reports headline performance metrics. It is deliberately data-source agnostic: bars from any sibling engine (`trade-data-equities`, `trade-data-options`, `trade-data-futures`, `trade-data-crypto`) are adapted structurally via `normalize_bar` -- this package never imports them, so each repo stays independently installable and the whole suite scales by composition.

## Design

One pass over the bar stream, six steps per timestamp:

```
bars(t) --> [fill pending orders at bar(t) open]
         --> [apply corporate actions with ex-date == bar(t) date]
         --> [mark portfolio to market]
         --> [accrue borrow cost on shorts]
         --> [strategy.on_bar(t) -> signals]
         --> [signals -> orders, held for bar(t+1)]
         --> [record equity point]
```

**No lookahead bias, by construction.** Orders signaled on bar *t* fill at bar *t+1*'s open. A strategy can never trade at a price it just observed.

### Components

| Module | Role |
|---|---|
| `models` | Immutable events: `Bar`, `Signal`, `Order`, `Fill`, `Trade`, `EquityPoint` |
| `data` | `BarDataHandler` ABC, `ListDataHandler`, `normalize_bar` adapter |
| `strategy` | `Strategy` ABC: `on_bar(timestamp, bars) -> [Signal]` |
| `portfolio` | `Portfolio` (cash, positions, FIFO trades), `PositionSizer` ABC, `FixedQuantitySizer`, `PercentEquitySizer` |
| `execution` | `SimulatedExecutionHandler`, commission models (`Flat`/`PerShare`/`Percent`), entry/exit slippage in bps, half-spread |
| `costs` | `CostModel` (default-on assumptions), `fill_cost` decomposition |
| `total_return` | `AdjustmentBasis`, `CorporateAction`/`Dividend`/`Split`, `AdjustedDataHandler`, `buy_and_hold_curve`, `excess_vs_benchmark` |
| `performance` | Pure metric functions + `summarize()` |
| `engine` | `BacktestEngine` event loop, `BacktestResult` (+ required `assumptions`) |

### Interoperability

Sibling engines expose bar-like objects (`Bar`, `FuturesBar`, `CryptoBar`, option quotes). `normalize_bar` accepts any of them -- a dict, or an object with `timestamp/open/high/low/close/volume` attributes:

```python
from trade_backtest import normalize_bar, ListDataHandler

bars = [normalize_bar(b) for b in equity_client.get_bars("AAPL", ...)]
bars += [normalize_bar(b, symbol="ESZ26") for b in futures_client.get_bars("ES", ...)]
data = ListDataHandler(bars)  # multi-symbol, grouped by timestamp
```

Timestamps are normalized to UTC; symbols to uppercase. Mixed portfolios (equities + futures + crypto + options in one run) work out of the box because everything downstream only sees the internal `Bar`.

### Scaling notes

- The engine streams `(timestamp, {symbol: bar})` pairs -- memory is O(symbols), not O(bars). A custom `BarDataHandler` can page from disk or a database for long histories.
- Strategies keep their own indicator state; the engine never copies bar history.
- `performance` functions are pure and vectorization-free, so results are trivially parallelizable across parameter grids (one process per backtest).

## Quick start

```python
from trade_backtest import (
    BacktestEngine, FixedQuantitySizer, ListDataHandler,
    Portfolio, SimulatedExecutionHandler, Strategy,
    Signal, SignalAction, normalize_bar,
)

class BuyAndHold(Strategy):
    def on_bar(self, ts, bars):
        if not getattr(self, "_bought", False):
            self._bought = True
            return [Signal(self.symbols[0], ts, SignalAction.LONG)]
        return []

engine = BacktestEngine(
    data=ListDataHandler([normalize_bar(b) for b in my_bars]),
    strategy=BuyAndHold(["AAPL"]),
    portfolio=Portfolio(100_000.0, FixedQuantitySizer(100)),
    execution=SimulatedExecutionHandler(slippage_bps=1.0),
    # Required: declare what your bars are. "none" = the asset class has
    # no corporate actions (crypto/futures/demo). Never silent.
    adjustment_basis="pre_adjusted",
    adjustment_note="vendor bars assumed split/dividend-adjusted",
)
result = engine.run()
print(result.final_equity, result.metrics["sharpe_ratio"])
for name, value in result.metrics.items():
    print(f"{name:28s} {value:+.4f}")
```

See `examples/sma_crossover.py` for a complete runnable SMA-crossover demo.

## Signals, sizing, and orders

Signals express **targets**, not orders:

- `LONG` -> target a long position (sized by the position sizer)
- `SHORT` -> target a short position
- `EXIT` -> flatten

The portfolio orders only the delta from the current position, so reversals (long -> short) work in one step. `FixedQuantitySizer` targets a fixed unit count scaled by signal `strength` (0..1 conviction); `PercentEquitySizer` targets a fraction of equity with an optional leverage cap. Set `Signal.limit_price` for limit entries/exits; the simulator fills when the bar's range touches the limit.

## Costs

Costs are **default-on**: a bare `SimulatedExecutionHandler()` applies
`CostModel.defaults()` -- 5 bps slippage each way, 1 bp half-spread,
$0.005/share commission, 50 bps/yr borrow on shorts (actual/365).
Every fill is decomposed into `commission + slippage + spread`
(`Fill.total_cost`); slippage moves the fill *price* (buys lift, sells
hit), the half-spread is charged to cash separately so the two never
double-count.

- **Entry vs exit slippage**: separate knobs. The portfolio marks each
  order `is_entry` (opens/increases = entry, closes/reduces = exit;
  reversals use the dominant leg -- documented approximation).
- **Borrow**: accrues daily on short market value, debited from cash,
  totaled in the assumptions block.
- **Opt-out**: `CostModel.disabled("<reason>")` -- the reason is
  mandatory; silent zero-cost runs are not a thing.
- **Legacy**: passing old-style `slippage_bps` and/or `commission`
  explicitly selects the legacy path (zeroed model + only what was
  passed), so callers with their own post-hoc cost models stay exact.
- **Not modeled**: market impact (a hook, `None` = stated as not
  modeled), partial fills, latency, margin calls.

## Assumptions

Every `BacktestResult` carries a required `assumptions` dict -- no
report without it:

```python
result.assumptions["adjustment_basis"]        # "pre_adjusted" | "unadjusted_with_events" | "none"
result.assumptions["corporate_actions_applied"] # {"dividends": n, "splits": n}
result.assumptions["cost_model"]               # full model: knobs, disabled flag + reason
result.assumptions["borrow"]                  # {"annual_bps", "total_paid", "day_count": "actual/365"}
result.assumptions["not_modeled"]             # ["market impact (...)", "partial fills", "latency", "margin calls"]
```

The engine *requires* `adjustment_basis` at construction (missing /
`None` / unknown -> `BacktestError`). Pass `corporate_actions=[...]`
with basis `"unadjusted_with_events"` and the engine backward-adjusts
splits (`AdjustedDataHandler`) and routes dividends through cash
(credited to longs, *paid* by shorts, reinvested at the ex-date close
by default). Compare against `buy_and_hold_curve(...)` -- the
total-return benchmark -- via `excess_vs_benchmark(...)`. See
[docs/COSTS_AND_TOTAL_RETURN.md](docs/COSTS_AND_TOTAL_RETURN.md) for
the rationale and migration guide.

## Metrics

`summarize()` returns: `total_return`, `cagr`, `annualized_volatility`, `sharpe_ratio`, `sortino_ratio`, `max_drawdown`, `max_drawdown_duration_bars`, `calmar_ratio`, `num_trades`, `win_rate`, `profit_factor`, `expectancy`. Pass `periods_per_year` (252 daily, 52 weekly, 8_760 hourly for crypto). Ratios use simple returns; Sharpe/Sortino are 0 when volatility is zero.

## Deliberate limitations

- **No margin model.** Shorts are allowed and cash may go negative; margin calls belong in `trade-risk`. Borrow *cost* on shorts is modeled (50 bps/yr default, actual/365); borrow on leveraged long cash balances is not.
- **No partial fills**, no market impact beyond the slippage bps and the half-spread model -- large orders look cheaper here than they are. A `market_impact` hook exists on `CostModel`; `None` means not modeled, stated in every report.
- **Daily granularity assumed for `periods_per_year=252`** -- set it explicitly for intraday/crypto.
- **Dividend reinvestment at the ex-date close is an approximation** (real reinvestment faces the same frictions as any trade).
- **Reversal orders use the dominant-leg slippage knob** -- a reversal really pays exit slippage on the closing leg and entry slippage on the opening leg.
- Single-threaded event loop; parallelize across backtests, not within one.

## Installation

```bash
pip install trade-backtest
```

Requires Python 3.10+. No dependencies.

## Testing

```bash
pip install -e .
pytest
```

90 tests, including a hand-computed 10-bar integration scenario that pins fill prices to the bar *after* each signal, hand-computed dividend/split/borrow scenarios, and the full cost-model matrix (default-on, opt-out, legacy).

## Versioning

Semantic versioning with a changelog. See [CHANGELOG.md](CHANGELOG.md).

## Monetization readiness

This repo follows the trade-suite standard: license-key hook stub, update-check hook, and a merchant-of-record (Gumroad/Lemon Squeezy) pattern live in the `trade-suite` meta-repo -- never custom billing code inside engines.

## The maths

**What you learn.** What a strategy *would have done* on your bars — fill
prices, costs, equity path, and a full metric panel — computed by an
event-driven loop that cannot peek at the future.

**Why it matters.** Backtests lie most often through lookahead bias and
ignored costs. This engine's maths is built to make both structurally hard:
signals from bar *t* can only fill at bar *t+1*'s open, and every fill pays
adverse slippage plus commission before it touches the equity curve.

**The maths.**

- *Event loop*: per timestamp — fill pending orders at the bar's open →
  mark to market → `strategy.on_bar(t)` → convert signals to orders held
  for bar *t+1* → record the equity point. Signals express *targets* (LONG /
  SHORT / EXIT); the portfolio orders only the delta from the current
  position, and FIFO accounting splits closing commissions across legs.
- *Costs*: slippage in basis points applied adversely (buys lift, sells
  hit) with separate entry/exit knobs -- the portfolio marks each order
  `is_entry` (dominant-leg rule for reversals); commission models are
  Flat / PerShare / Percent, computed on the raw fill price; the
  half-spread (default 1 bp per side) is charged to cash, never baked
  into the price, so `Fill.total_cost = commission + slippage + spread`
  decomposes exactly.
- *Borrow*: short market value x `borrow_cost_annual_bps / 10000` x
  `days / 365` (actual/365), debited from cash each bar and accumulated
  in `Portfolio.total_borrow_cost`.
- *Total return*: `TR = price return + dividend yield effects`. Splits
  backward-adjust pre-split bars (`O/H/L/C / ratio`, `volume x ratio`,
  compounding across splits); dividends never touch bars -- on the
  ex-date, longs are credited `qty x amount` (reinvested into new shares
  at the close by default, commission-free) and shorts *pay* `|qty| x
  amount` from cash. The buy-and-hold benchmark buys at the first open
  and holds with the same dividend logic; `excess_vs_benchmark` compares
  aligned curves.
- *Sharpe*: annualized on simple returns — `mean(excess) / pstdev(excess) × √252`,
  with the risk-free rate converted per-period as `(1 + rf)^(1/252) − 1`; 0
  when volatility is zero.
- *Sortino*: same numerator, but the denominator is downside deviation
  `√(mean(min(0, r)²))` — upside volatility is not punished.
- *Max drawdown*: the worst peak-to-trough loss as a positive fraction,
  plus its duration in bars; *CAGR* from first to last equity;
  *Calmar* = CAGR / max drawdown; *profit factor* = gross wins / gross
  losses; *expectancy* = mean trade P&L.
- *Sizing*: `FixedQuantitySizer` scales a fixed unit count by signal
  `strength` (0–1 conviction); `PercentEquitySizer` targets a fraction of
  equity with an optional leverage cap.

**Honest limitations.**

- No margin model: shorts are allowed and cash may go negative; margin
  calls belong in trade-risk. Borrow cost on shorts is modeled
  (actual/365); borrow on leveraged long cash is not.
- No partial fills and no market impact beyond slippage bps plus the
  half-spread model -- large orders look cheaper here than they are. The
  `market_impact` hook exists; `None` means not modeled, stated in every
  report.
- Dividend reinvestment at the ex-date close is an approximation; real
  reinvestment faces the same frictions as any trade.
- Reversal orders use the dominant-leg slippage knob -- a reversal
  really pays exit slippage on the closing leg and entry slippage on the
  opening leg.
- `periods_per_year=252` assumes daily bars; set it explicitly (e.g. 8,760
  for hourly crypto) or every annualized number is wrong.
- Single-threaded event loop — parallelize across backtests, not within one.

## License

MIT. See [LICENSE](LICENSE).
