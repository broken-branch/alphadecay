# Architecture

alphadecay has a FastAPI service, a React front end, a deterministic options policy, PostgreSQL storage, and small adapters for outside services. The public Replay runs the complete path from a browser request to a policy result. A development rehearsal has also exercised the real provider path, using only requests that read data, without changing the paper account.

## Replay

```text
Browser
  -> React Replay screen
  -> POST /api/replays/{scenario}
  -> fixed fixture
  -> exposure and drift calculations
  -> HOLD, CLOSE, ROLL, or NO_ACTION policy
  -> assessment record
  -> execution disabled: no order sent
```

Replay includes four examples:

- `THESIS_INTACT` ends in `HOLD`;
- `THETA_TAKEOVER` presents a bounded `ROLL` candidate;
- `CATALYST_BROKEN` ends in `CLOSE`;
- `STALE_QUOTE` returns `NO_ACTION` because its quote is too old.

These are invented examples. They contain no current market data, account identifier, or broker result.

## Competition records

`GET /api/competition-record` reads a deliberately published lifecycle history. It can show a clean `NO_TRADE` result. If a position exists, it can instead show the saved thesis, later observations, policy decisions, and reconciled paper outcome.

`GET /api/proof` reads the latest eligible account performance snapshot. It is kept separate from the position history. Neither route calls Alpaca, accepts a result chosen by the caller, or presents missing data as zero.

The owner publication routes accept no request body and no snapshot selector. They publish only eligible database records after the owner session, origin, and CSRF checks pass. Public responses omit account, order, position, activity, and provider identifiers.

## Who makes the decision

The model does not choose the action. Application code selects the research calls and prepares a small evidence set. Gemini may label only the supplied source IDs using a fixed response format. Regular code calculates exposure and drift, applies the policy, checks the rejected alternatives, and builds the record shown in the browser.

If the evidence has not changed, alphadecay reuses the earlier stored classification. Missing or invalid model output cannot approve an action.

## Alpaca connections

### Trading API

Typed adapters read the paper account, positions, orders, and option data. They also handle option limit orders and reconciliation. Before any broker attempt, the execution service stores the exact intent and assigns a stable client order ID. If the result of a write is uncertain, the service looks up that order instead of sending it again. Expected exposure is never reported as a broker result.

These parts are tested with fakes and PostgreSQL. [`PROVIDER_REHEARSAL_PROOF.json`](PROVIDER_REHEARSAL_PROOF.json) records a real development run that left the account book unchanged and made no writes. The public evidence does not yet include a positive managed position assessment, a real order write, or a broker reconciliation.

### MCP server

The backend may launch the pinned Alpaca MCP executable without a shell. The application gives it a fixed environment, checks its available tools, and permits only research calls selected by the application that cannot change an account. Trading, account changes, watchlists, and locate tools are excluded.

MCP never receives permission to execute a trade, and the model cannot choose an MCP tool.

### CLI

The Alpaca CLI is used only by an operator for bootstrap and inspection work. It is not imported by the backend, included in the production image, or callable from the browser, model, or scheduler. [`CLI_PROOF.json`](CLI_PROOF.json) records a dry run for an option order with two legs against Alpaca's paper host. It did not submit the order.

The provider rehearsal brings the Trading API, MCP, and CLI checks together in one receipt without account identifiers. It is labeled `DEVELOPMENT` and is not competition evidence.

## Access and safety boundaries

- Anonymous visitors can run Replay and read records that were deliberately published. They cannot retrieve a paper account or reach code that changes one.

- Owner authentication permits a small set of run, publication, and autonomy controls. The owner can arm or disarm the persistent account gate only when the server gate allows it. These controls cannot select or widen an order, and they do not bypass policy.

- Only an authenticated scheduler request can dispatch a stored intent after both autonomy gates are on. The scheduler cannot arm itself.

- Provider credentials stay in server environment variables.

- The Alpaca host is fixed to paper trading.

- Replay, development records, and competition records remain separate.

- Public records use sanitized fields and hashes instead of broker identifiers.

## Deployment

The deployment files describe one Docker web service and one PostgreSQL database on Render. The image builds the React app, installs the locked Python packages, runs as an unprivileged user, and serves the front end and API from one process.

The public app is [alphadecay.onrender.com](https://alphadecay.onrender.com). Its health response gives the deployed Render commit and runtime mode. The app and repository can be checked without signing in. A working deployment does not prove an order, fill, reconciliation, or return.
