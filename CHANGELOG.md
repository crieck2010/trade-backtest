# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
