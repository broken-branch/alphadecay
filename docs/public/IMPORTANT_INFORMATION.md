# Important information

alphadecay is a hackathon prototype for a narrow paper options workflow. Replay explains the decisions with fixed examples. The connected agent can record what happened in an Alpaca paper account.

## Paper trading only

alphadecay works only with Alpaca paper trading. It rejects every other Alpaca trading endpoint and has no route for a live brokerage account.

Replay uses invented examples and sends no order. Any account result shown by the product must be labeled as paper trading. A simulated fill does not show that the same order would fill, or fill at the same price, in a live market.

## No investment advice

alphadecay is not an investment adviser, recommendation service, or broker. Its examples and paper account records are not instructions to buy, sell, or hold a security.

Options can lose money. A defined risk position limits its planned loss, but it cannot guarantee a profit. Replay, tests, paper fills, competition results, and model classifications do not promise future performance.

## Prototype limits

The product returns `NO_ACTION` or `NO_TRADE` when required data, authority, or reconciliation is missing or uncertain. That is normal behavior. The model can label a supplied evidence set, but application code applies the policy and safety checks.

Market data may be delayed, indicative, stale, incomplete, or wrong. Paper trading omits market impact, order queue position, latency, price improvement, regulatory fees, and other conditions of live trading.

The public demo may be briefly unavailable while its hosting service restarts.

## Outside services

alphadecay uses Alpaca, Render, Google Gemini, and GitHub. Each service has its own terms, privacy policy, availability, and data practices. Their use in this prototype is not a warranty or endorsement.
