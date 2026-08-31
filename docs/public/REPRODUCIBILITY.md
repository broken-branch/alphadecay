# Verify the project

The repository provides separate checks for code, Replay, provider connections, deployment, and competition results. Each section below says what its evidence does and does not establish.

## Check the repository

Install the locked dependencies:

```bash
uv sync --python 3.12 --frozen --all-groups
npm ci
```

Run the backend, contract, and policy checks:

```bash
uv run --python 3.12 pytest -q
uv run --python 3.12 ruff check \
  backend ops/contracts \
  ops/release/generate_third_party_notices.py \
  ops/release/test_third_party_notices.py
uv run --python 3.12 python -m ops.contracts.generate_openapi
git diff --exit-code -- contracts/openapi-v1.json
```

Run the browser and license checks:

```bash
npm test -- --run
npm run typecheck
npm run build
uv run --python 3.12 python -m ops.release.generate_third_party_notices --check
```

The notice check matches every Python and browser package shipped with the app to the installed lock version. It fails when a package is missing, its license is unknown or incompatible, or the checked notice has changed.

Check public copy, links, and the Render Blueprint:

```bash
python3 ops/quality/public_copy_check.py
python3 ops/quality/render_blueprint_check.py
python3 ops/quality/public_link_check.py --root . README.md docs/public/*.md
```

Some PostgreSQL tests skip when no local database is available. A release check should run them against a disposable PostgreSQL instance and record the database version with the result.

## Check Replay

Start the local app using [Setup](SETUP.md), then run all four examples. Keep this label visible:

```text
REPLAY · SAMPLE DATA · NO ORDER SENT
```

For each example, compare the API response with the page:

- fixture and policy hashes;
- intended, current, and expected exposure;
- selected action and rejected alternatives;
- the separate result showing that execution was disabled.

This checks the path from a fixed fixture to the policy result. It does not check current provider data, a paper order, or profit and loss.

## Check the competition strategy status

After reviewing the development results, we selected a bearish competition candidate and fixed it before opening its holdout once. Validation produced too few qualifying trades, and too much of the result depended on one trade. The candidate was rejected, so alphadecay sent no competition order.

[`COMPETITION_CHECKPOINT_PROOF.json`](COMPETITION_CHECKPOINT_PROOF.json) records that narrow result alongside a current account checkpoint. It omits the ticker, dates, private thresholds, provider records, and account identifiers. The Competition Record API remains `NOT_PUBLISHED` because no competition order was sent.

## Check the provider receipt

The repository includes typed Alpaca, MCP, and Gemini adapters with fixture tests. [`PROVIDER_REHEARSAL_PROOF.json`](PROVIDER_REHEARSAL_PROOF.json) is the sanitized receipt from one development account rehearsal. It records eleven provider requests that read data, none that changed it, an unchanged account book, one MCP `get_clock` call, and the pinned CLI dry run. The service stopped because it could not identify one managed position.

The receipt is bound to the local sealed summary and operator source hashes. Its private account and provider records are not published. It proves that the connections worked and that the service stopped safely. It does not prove a fill, a positive lifecycle assessment, a competition result, or profit and loss.

Do not run a credentialed smoke test as part of an anonymous reproduction, and do not publish a provider's raw response.

## Check broker and deployment claims

A broker claim requires a terminal paper order, no unresolved remainder, and reconciliation of the whole account in a record labeled with its account role. This revision includes the two autonomy gates and a development rehearsal that made no writes. It does not include a public terminal broker record or proof of a real autonomous order.

The public Render URL and GitHub repository are available without signing in. The health response gives the deployed Render commit and runtime mode. The Dockerfile and Render Blueprint let another person rebuild the service. The running demo proves deployment, not provider access, an order, or a return.

## Check performance claims

Competition performance can come only from the dedicated paper account and its sealed starting balance. The public response omits account, order, position, and activity identifiers. If no eligible trade exists, the accurate status is `NO_TRADE`. If a scheduled capture is missing or unusable, the page reports that instead of choosing an older result.

Paper performance is simulated. It is not evidence of live returns.
