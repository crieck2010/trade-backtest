# trade-backtest

An event-driven backtesting engine for the [trade-suite](https://github.com/crieck2010/trade-suite) algorithmic trading system. Pure Python, stdlib-only: no NumPy, no pandas, no data-vendor lock-in.

Strategies consume normalized OHLCV bars, emit signals, and the engine simulates execution with slippage and commissions, tracks cash and positions with FIFO accounting, and reports headline performance metrics. It is deliberately data-source agnostic: bars from any sibling engine (`trade-data-equities`, `trade-data-options`, `trade-data-futures`, `trade-data-crypto`) are adapted structurally via `normalize_bar` -- this package never imports them, so each repo stays independently installable and the whole suite scales by composition.

## Design

One pass over the bar stream, five steps per timestamp:

```
bars(t) --> [fill pending orders at bar(t) open]
         --> [mark portfolio to market]
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
| `execution` | `SimulatedExecutionHandler`, commission models (`Flat`/`PerShare`/`Percent`), slippage in bps |
| `performance` | Pure metric functions + `summarize()` |
| `engine` | `BacktestEngine` event loop, `BacktestResult` |

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

- **Slippage**: basis points, applied adversely (buys lift, sells hit).
- **Commissions**: `FlatCommission`, `PerShareCommission`, `PercentCommission`, `NoCommission`. Commission on a fill that closes multiple FIFO lots is split across the legs proportionally.

## Metrics

`summarize()` returns: `total_return`, `cagr`, `annualized_volatility`, `sharpe_ratio`, `sortino_ratio`, `max_drawdown`, `max_drawdown_duration_bars`, `calmar_ratio`, `num_trades`, `win_rate`, `profit_factor`, `expectancy`. Pass `periods_per_year` (252 daily, 52 weekly, 8_760 hourly for crypto). Ratios use simple returns; Sharpe/Sortino are 0 when volatility is zero.

## Deliberate limitations

- **No margin model.** Shorts are allowed and cash may go negative; margin calls and borrow costs belong in `trade-risk`.
- **No partial fills**, no market impact beyond slippage bps.
- **Daily granularity assumed for `periods_per_year=252`** -- set it explicitly for intraday/crypto.
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

53 tests, including a hand-computed 10-bar integration scenario that pins fill prices to the bar *after* each signal.

## Versioning

Semantic versioning with a changelog. See [CHANGELOG.md](CHANGELOG.md).

## Monetization readiness

This repo follows the trade-suite standard: license-key hook stub, update-check hook, and a merchant-of-record (Gumroad/Lemon Squeezy) pattern live in the `trade-suite` meta-repo -- never custom billing code inside engines.

## License

MIT. See [LICENSE](LICENSE).
