# API guide

The public API gives a reviewer three useful checks: run the fixed Replay policy, read the published competition timeline, and inspect the separate account checkpoint. The hosted [OpenAPI document](https://alphadecay.onrender.com/openapi.json) defines the response fields, and the browser-friendly [API reference](https://alphadecay.onrender.com/docs) exposes the same routes.

Set the deployment once:

```bash
BASE_URL=https://alphadecay.onrender.com
```

## 1. Run Replay

```bash
curl -fsS -X POST "$BASE_URL/api/replays/THETA_TAKEOVER" |
  jq '{scenario, action: .assessment.action, reason: .assessment.rationale_code, quality: .assessment.quality, execution_enabled, input_hash, assessment_hash}'
```

`THETA_TAKEOVER` returns the policy's sample `ROLL` decision. The other accepted names are `THESIS_INTACT`, `CATALYST_BROKEN`, and `STALE_QUOTE`, which produce the fixture's `HOLD`, `CLOSE`, and `NO_ACTION` paths. `execution_enabled` is `false` for every Replay response. The [fixture tests](../../backend/tests/integration/test_replay_api.py) pin the scenarios, hashes, and decisions.

Replay uses synthetic fixtures and does not ask you to supply a plan or direction. It does not call Alpaca, an AI provider, MCP, or the CLI, and it cannot place an order. The browser keeps its sample-data label visible.

## 2. Read the competition timeline

```bash
curl -fsS "$BASE_URL/api/competition-record" |
  jq '{publication_status, records: [.records[] | {kind, occurred_at, payload}]}'
```

This route contains published paper `NO_TRADE` decisions or position events in chronological order. A position record can include its opening, scheduled review, roll, and close. Each lifecycle review combines that position's record with the reconciled account evidence needed to observe it safely. `NOT_PUBLISHED` with an empty list means no record has been released; it does not imply a trade or a zero result. The [archive repository tests](../../backend/tests/competition_archive/test_repository.py) verify the publication boundary and sanitized response.

## 3. Read the account checkpoint

```bash
curl -fsS "$BASE_URL/api/proof" |
  jq '{publication_status, point}'
```

The checkpoint reports whether the dedicated paper-account measurement was published and whether it is complete, missing, or unknown. It is separate from the position timeline and is not an organizer score. The [proof contract tests](../../backend/tests/contracts/test_performance_proof_contract.py) cover the public shape and omission of account identifiers.

## Reading a response

In Replay, `assessment.action` is `HOLD`, `CLOSE`, `ROLL`, or `NO_ACTION`; `rationale_code` names the selected rule; and `quality` identifies complete, stale, missing, or unknown inputs. `input_hash` binds the fixture while `assessment_hash` binds the policy output. The `presentation` object contains judge-readable fields, and `certificate` contains the saved thesis, expected exposure, rejected alternatives, and execution state.

Competition records use `kind` to separate entry decisions from position events. The proof endpoint keeps account performance out of that lifecycle. Both routes return only data that the owner deliberately published through no-selector endpoints tested in the [competition archive suite](../../backend/tests/competition_archive).

The service health check remains available at `GET /api/health`. It reports the exact build and `REPLAY_ONLY` or `CONNECTED` runtime mode. For local use, replace `BASE_URL` with `http://127.0.0.1:8000`.

## Status and errors

These calls are deliberately read-only. A missing publication returns a stable status or `404`; it never falls through to an owner record. Invalid Replay names are rejected before policy evaluation. The response does not contain credentials or account, order, position, activity, or provider identifiers. You can verify the deployed source by comparing the commit from `/api/health` with the public repository commit.

The owner-only experiment performance route is documented in OpenAPI for a connected build, but it is not one of the three public proof calls. It requires the signed owner session, a matching request-protection token, and an allowed origin. Reading a private projection does not publish it.
