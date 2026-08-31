# Strategy Research Record

alphadecay was built to manage an options position after entry, but an autonomous agent also needs a defensible reason to open one. We tested several entry ideas during the hackathon. None met its rules for promotion to the competition account.

The result is simple: no competition order was sent. The account stayed at its original paper balance rather than taking a trade that the research did not support.

## What we tested

| Research family | What happened | Decision |
|---|---|---|
| Continuation after earnings | The development sample did not produce enough signals to justify opening its holdout. | Rejected |
| Later earnings candidates | One candidate failed development. Another was rejected because its validation data had been fetched before the holdout was formally opened. | Rejected |
| SPY signals after the open | The broad underlying price study lost money after estimated costs. A later bearish test produced only three holdout trades, below the required twelve. One winner accounted for 56.4% of gross gains. | Rejected |
| SPY opening drive | The strongest underlying price version averaged 0.079% per trade under the moderate cost assumption and lost money under heavier costs. A few winners drove most gains, and results were not stable across time. | Rejected |
| SPY reaction to macro releases | Across twenty historical signals, the compounded underlying price proxy gained 3.22%. Only 30% of trades were profitable; the median trade and one calendar year were negative. | Rejected |
| Sector ETF opening dislocations | Across 486 trades in nine sector ETFs, the compounded underlying price proxy lost 12.85% with moderate estimated costs and 46.42% with heavier costs. Only one of five calendar years was positive, and bearish trades lost money. | Rejected |

These figures come from historical prices for SPY, sector ETFs, or individual stocks. They are not historical option returns or evidence of executable option fills.

## Why the rejected work matters

Every valid final evaluation used rules fixed before the holdout was opened. If holdout integrity was lost, we rejected the study instead of reusing the data. Candidates also had to survive estimated costs, minimum sample size, time stability, drawdown, and dependence on a few winners.

That same rule applies inside the product. A model may label new evidence, but it cannot relax the entry policy, choose the risk, or send an order. When the evidence is not good enough, `NO_TRADE` is the result.

The public Replay demonstrates the part that remains useful once a position exists: alphadecay compares the opening thesis with the position now, measures exposure and time pressure, and chooses `HOLD`, `CLOSE`, `ROLL`, or `NO_ACTION` under fixed risk rules.

## Limits

The data available to this project did not include the complete historical option quotes and Greeks needed to reconstruct a trustworthy options fill. The studies therefore tested direction and timing on the underlying security. Any future candidate would still need current option liquidity, spread, Greek, account, and maximum-loss checks before a paper order.

Backtests are hypothetical. They depend on the selected data, event definitions, fill timing, and trading-friction assumptions. They do not predict future results.
