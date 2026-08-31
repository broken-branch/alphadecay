# Limitations

alphadecay is a hackathon prototype for paper options trading. These are its current limits.

## What has been proven

- Replay is tested against the fixtures in this repository. It sends no order.

- After reviewing development results, a bearish competition candidate was fixed before its holdout was opened. It produced too few qualifying trades, and too much of the result depended on one trade. It was not promoted, and no competition order was sent.

- A development account rehearsal exercised the Trading API, MCP, and CLI paths. Every provider request only read data, and the account book stayed unchanged. The run stopped because it could not identify one managed position.

- The route that lets an owner arm the scheduler has been rehearsed with a development paper account. The account book did not change.

- The broker service is covered by tests using fakes and PostgreSQL. This revision does not provide public proof of a real autonomous order.

- The Render app and GitHub repository are available without signing in. That proves the demo can be reached. It does not prove a broker connection, order, or return.

The development rehearsal does not prove a positive assessment, fill, reconciliation, competition result, or profit and loss.

## Market data

The free Alpaca options feed is indicative rather than OPRA. Quotes may be missing, stale, or crossed. Greeks and implied volatility may also be absent, especially when there is no usable bid and ask. alphadecay treats missing execution data as unknown and stops the action. It never replaces a missing value with zero.

Historical Alpaca option data does not include the old bid, ask, and Greek record needed to recreate a complete options decision. Research on historical direction cannot prove that the option structure or fill would have worked.

## Paper trading

Alpaca paper fills are simulations. They omit market impact, queue position, latency slippage, price improvement, regulatory fees, and dividends. Paper results do not predict live performance.

alphadecay has no live trading setting. It rejects every Alpaca trading endpoint except the paper endpoint.

## Supported strategy

The policy manages vertical spreads with a defined maximum loss and one expiration date. It does not support naked short options, entries on expiration day, market orders, separate leg orders, bulk closing or cancellation, exercise, or instructions not to exercise.

The policy returns `NO_ACTION` when data, authority, or reconciliation is uncertain. It returns `NO_TRADE` when an entry check does not pass. Both are normal results.

## Model and research sources

Gemini receives a small sanitized evidence object and returns a bounded classification. The service may be unavailable, and its answer may be wrong. The model cannot calculate Greeks, select tools, choose an action, write browser copy, or place an order. The fixed policy and data checks decide the result.

MCP research is limited to calls selected by the application that cannot change an account. A source headline is evidence, not a statement verified by alphadecay.

## Operations

The hosted app can restart, and scheduled GitHub Actions can run late. The backend checks the market time and data age on every run. A late start never permits an action based on stale data.

The public performance endpoint shows only a sanitized record that the owner deliberately published. It is not the organizer's scoring formula, which has not been published.

## Use

alphadecay is not investment advice. Its paper results and Replay examples are not promises of live returns.
