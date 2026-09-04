# Strategy research

| Research family | Measured result | Decision |
|---|---|---|
| Continuation after earnings | The development sample did not produce enough signals to justify opening its holdout. | Rejected |
| Later earnings candidates | One candidate failed development. Another used validation data fetched before the holdout was formally opened. | Rejected |
| SPY signals after the open | The broad underlying-price study lost money after estimated costs. A later bearish test produced three holdout trades, below the required twelve. One winner accounted for 56.4% of gross gains. | Rejected |
| SPY opening drive | The strongest underlying-price version averaged 0.079% per trade under the moderate cost assumption and lost money under heavier costs. Results were not stable over time. | Rejected |
| SPY reaction to macro releases | Across twenty historical signals, the compounded underlying price proxy gained 3.22%. Only 30% of trades were profitable; the median trade and one calendar year were negative. | Rejected |
| Sector ETF opening dislocations | Across 486 trades in nine sector ETFs, the compounded underlying price proxy lost 12.85% with moderate estimated costs and 46.42% with heavier costs. Only one of five calendar years was positive, and bearish trades lost money. | Rejected |
| Weekly SPY put spreads | Across 69 development trades, the study lost $1,187.77 under its base assumptions, lost money in both years, and had a 0.57 profit factor. Heavier costs reduced the profit factor to 0.28. | Rejected |

AlphaDecay did not promote these ideas to the competition account. The dated record carries the competition outcome; a backtest does not.

## Rejection rules

Each final evaluation used rules fixed before its untouched test period began. Previously seen data disqualified the test. Candidates also had to survive estimated costs, sample, time, drawdown, and concentration checks. The 56.4% figure shows why a positive total was not enough.

Most rows use historical SPY, sector ETF, or stock prices rather than option returns. “Compounded underlying price proxy” compounds directional moves in the underlying; it is not options profit and loss. “Profit factor” is gross gains divided by gross losses. Below 1 means losses were larger.

The weekly spread row uses Alpaca historical option trade bars, which are derived and delayed rather than executable quotes. The candidate still failed its loss and stability rules.

## What carries into the product

The same discipline governs a scheduled run. The model may label new evidence in fixed fields, but it cannot relax an entry condition after seeing the market. Code checks the frozen rule, option liquidity, spread, Greeks, account state, authority, and maximum loss. A failed requirement is recorded.

Once a position exists, Replay asks whether the opening reason, time, or exposure now requires `HOLD`, `CLOSE`, `ROLL`, or `NO_ACTION`. The [Replay test](../../backend/tests/integration/test_replay_api.py) pins those samples.

Backtests depend on the selected data, event definitions, timing, and cost assumptions. They are hypothetical and do not predict future results. [Limitations](LIMITATIONS.md) explains the quote and paper-fill boundaries.
