# tradeterm — 100% automated PAPER trading terminal

**Paper only. No real money, no exchange accounts, no API keys, $0.** Every account starts with a virtual $20.
Live dashboard: https://charlesanthonyleone.github.io/tradeterm/ (data.json is force-pushed to `gh-pages` every 10 min as one squashed commit) · local: http://127.0.0.1:8955/

## What runs
Nine parallel paper accounts (`accounts.json`), all on the same live Coinbase public ticker and candles (Kraken public is the fallback). Pairs: BTC, ETH, SOL, DOGE.
Shared risk rules (`config.json`): at most 50% of equity per position, 2 open/pending, 6 entries/day, halt at -5% daily loss, 30 min cooldown (120 after a loss), 0.05% slippage on taker fills, and exchange minimum order sizes.

| Account | Strategy | Fees (maker/taker) | Attainable at $20? |
|---|---|---|---|
| baseline | EMA9/21 cross + RSI 50–70, 5m, TP 2% / SL 1%, taker | 0.40/0.60 (original spec) | n/a (older schedule) |
| ema_cb_us_maker | same rules, limit orders | Coinbase US entry 0.50/0.90 | yes |
| ema_kraken_t1_maker | same | Kraken Pro Tier 1 0.40/0.80 | yes |
| ema_kraken_t3_maker | same | Kraken Tier 3 0.22/0.38 | no ($10K vol or $20K on platform) |
| ema_kraken_t12_maker | same | Kraken Tier 12 0.00/0.10 (lowest maker) | no ($10M vol) |
| swing_1h_cb_us / _kraken_t1 | EMA20/50 cross + RSI, 1h, TP 6% / SL 3%, hold ≤96h | CB US / Kraken T1 | yes |
| meanrev_15m_kraken_t1 / _cb_us | buy RSI<30 on 15m if gap to SMA20 > round-trip cost; exit at SMA20, SL 3%, hold ≤8h | Kraken T1 / CB US | yes |

Every entry also requires the expected move (or the gap to the mean) to exceed round-trip cost. Maker fills are modeled honestly: a limit order fills only if price trades *through* it within one bar, and otherwise it's cancelled. Stop-losses always pay taker fees plus slippage.

**Fee sources (checked 2026-11-02):** Coinbase cut fees on Sep 16 2026 to a US entry tier of **0.50% maker / 0.90% taker**, so the spec's 0.4/0.6 is now outdated ([Coinbase blog](https://www.coinbase.com/blog/were-lowering-fees-for-many-active-traders-on-coinbase-advanced)). Kraken Pro spot: Tier 1 0.40/0.80, Tier 3 0.22/0.38, Tier 12 0.00/0.10 ([Kraken fee schedule](https://www.kraken.com/features/fee-schedule)). Zero-fee stablecoin pairs (Coinbase's 0% maker stable pairs, Kraken USDG/USDe) only trade pegged assets that barely move, so there's nothing to scalp and they aren't used.

## Research method (`python3 multi.py research`)
45 days of public candles. **Full**: fixed rules over days 4–45. **Walk-forward**: a rolling 21-day train window picks TP/SL multipliers (EMA) or SL/RSI threshold (mean-reversion) from a small grid, using only train-window net P&L. The choice is then applied to the next untouched 7-day test window, and the results are summed out-of-sample (OOS). Results are in `reports/research.json`.

## Results (USD on $20, 45 days to 2026-11-02)
| Account | RT cost | Full: trades / win% / gross / fees / **net** | max DD | WF-OOS: trades / gross / fees / **net** |
|---|---|---|---|---|
| baseline | 1.30% | 0 / – / 0 / 0 / **0.00** | 0% | 0 / 0 / 0 / **0.00** |
| ema_cb_us_maker | 1.02% | 1 / 0 / -0.11 / 0.14 / **-0.24** | 1.2% | 0 / 0 / 0 / **0.00** |
| ema_kraken_t1_maker | 0.82% | 13 / 69% / +1.35 / 1.19 / **+0.16** | 3.8% | 6 / +0.93 / 0.56 / **+0.37** |
| ema_kraken_t3_maker | 0.46% | 82 / 31% / +0.71 / 4.25 / **-3.54** | 17.8% | 44 / +1.29 / 2.50 / **-1.22** |
| ema_kraken_t12_maker | 0.02% | 251 / 32% / -3.84 / 1.99 / **-5.83** | 29.3% | 149 / -2.01 / 1.36 / **-3.37** |
| swing_1h_cb_us | 1.02% | 16 / 6% / -2.33 / 1.98 / **-4.32** | 23.5% | 11 / -1.51 / 1.46 / **-2.97** |
| swing_1h_kraken_t1 | 0.82% | 16 / 6% / -2.21 / 1.72 / **-3.93** | 21.7% | 11 / -1.41 / 1.25 / **-2.66** |
| meanrev_15m_kraken_t1 | 0.82% | 75 / 17% / -1.36 / 5.31 / **-6.67** | 33.7% | 30 / +0.20 / 2.52 / **-2.32** |
| meanrev_15m_cb_us | 1.02% | 64 / 9% / -1.11 / 5.62 / **-6.73** | 34.0% | 26 / -0.04 / 2.71 / **-2.75** |

## Honest verdict
- **Fees break small-margin scalping at every fee tier a $20 account can get.** The one positive line (Kraken T1 maker, +$0.16 full, +$0.37 OOS) comes from 13 and 6 trades. That's noise, not an edge. Lower fees didn't rescue it: as costs fell, the cost filter let more trades through, and those trades were losers *before* fees (T12 gross -$3.84). So the signal itself has no edge on 5-minute data.
- **Swing on 1h:** expected moves do clear costs, but in this 45-day window (a downtrend) the signal lost money before fees (1 win in 16).
- **Mean reversion:** it buys falling prices, and the mean keeps moving down after entry, so "exit at mean" often lands below the entry price. Fees then make it the worst variant.
- Expect every account to stay flat or slowly lose money. Nothing here justifies real money.
