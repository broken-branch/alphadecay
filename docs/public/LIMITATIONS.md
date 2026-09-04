# Limitations

AlphaDecay is a hackathon prototype for one narrow options experiment. The public demo, development receipts, and competition record answer different questions and should not be treated as interchangeable evidence.

## Evidence

Replay proves that fixed repository inputs produce the displayed `HOLD`, `CLOSE`, `ROLL`, and `NO_ACTION` decisions. It uses invented data. The [development receipt](PROVIDER_REHEARSAL_PROOF.json) proves that eleven read-only provider requests completed and the development account stayed unchanged. It does not prove an order. Only a deliberately published [competition timeline](https://alphadecay.onrender.com/api/competition-record) and [account checkpoint](https://alphadecay.onrender.com/api/proof) can support a judged-account claim.

An unavailable value stays unavailable. `NOT_PUBLISHED` means no eligible result has been released. A deployment health response proves that the app is running, not that providers are connected or that an order filled.

## Data and simulated trading

Alpaca's free options feed is indicative rather than OPRA. Quotes can be missing, old, crossed, or inconsistent. Greeks and implied volatility may be absent when there is no usable bid and ask. The policy treats missing execution data as unknown and stops.

Historical option data does not contain every old executable bid, ask, and Greek needed to recreate a trustworthy options fill. Most strategy studies therefore test direction on the underlying security. The weekly spread study uses derived and delayed option trade bars. Its limits and losses are recorded in [Strategy research](STRATEGY_RESEARCH.md).

Paper fills omit market impact, order-queue position, latency slippage, price improvement, regulatory fees, and dividends. They do not show that a live order would fill at the same price, and they do not predict future performance.

## Product scope

The policy supports one underlying, defined-risk call or put verticals, one expiration, and a bounded quantity and risk limit fixed by each plan. A reconciled open book may admit another eligible plan only when its controls allow it; each position still has a separate lifecycle. It excludes naked short options, expiration-day entries, market orders, separate-leg execution, bulk closing or cancellation, exercise, and do-not-exercise instructions.

The experiment workspace can collect a thesis, accept bounded model labels, preserve a reviewed definition, compile fixed rules, and show a performance record. User-authored experiments are not automatically scheduled or connected to the launch runtime in this release.

The model can classify the supplied thesis and named evidence incorrectly or be unavailable. It does not calculate Greeks, set risk, select a contract, choose an action, or write the public explanation. Fixed code performs those jobs. A source headline is attributed evidence, not a fact verified by AlphaDecay.

## Operations and privacy

The hosted service can restart, and scheduled requests can arrive late. Every run checks the market clock and data age before acting. Public records omit account, order, position, activity, and provider identifiers. Replay writes no browser storage; the only cookies are short-lived owner authentication and request-protection cookies. The full policy is in [Privacy](reviewers/PRIVACY.md), and connected deployment requirements are in [Setup](reviewers/SETUP.md).

AlphaDecay is not investment advice, a recommendation service, or a broker. Options can lose money even when their planned maximum loss is defined. Provider availability, paper records, tests, and model classifications are not warranties or endorsements.
