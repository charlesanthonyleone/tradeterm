# tradeterm — 100% automated PAPER trading terminal

**Paper only. No real money, no exchange accounts, no API keys, $0.** Starts with a virtual $20.

- Data: Coinbase Exchange public candles/ticker (Kraken public fallback). Pairs: BTC, ETH, SOL, DOGE vs USD.
- Every fill: taker 0.6% / maker 0.4% fee (configurable), live bid/ask spread, +0.05% slippage, min order size / base increment from Coinbase product data.
- Strategy `ema_cross_rsi_v1` (all figures in `config.json`): 5-minute candles, EMA9 crosses above EMA21, RSI(14) between 50 and 70, TP +2.0%, SL -1.0%, max hold 48 bars (4 h), 50% of equity max per position, max 2 open, 6 entries/day, halt at -5% daily loss, 30 min cooldown (120 after a loss), and **trade only if the expected move (ATR·√6) ≥ 1.5× round-trip cost**.
- `python3 tt.py backtest 7` runs the backtest · `./run.sh` starts daemon + dashboard (127.0.0.1:8955) · `./stop.sh` stops it · `python3 report.py` writes reports/report.html + equity.png
- The daemon pushes `data.json` to the `gh-pages` branch every 10 min as one force-pushed commit, so the history gets squashed and old snapshots don't pile up.

## Honest verdict
At Coinbase's lowest-tier fees, **small-margin scalping is not profitable**. A round trip costs about 1.3% (0.6% + 0.6% + spread + slippage), and 5-minute moves on these pairs are usually 0.4–0.6%. In the 7-day backtest the strategy without the cost filter was roughly break-even *before* fees and lost heavily after them, and maker fees didn't change that. With the cost filter on, it correctly takes almost no trades. The expected result is that the account stays flat or bleeds slowly.
