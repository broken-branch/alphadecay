# Quick tour and API guide

You can look through alphadecay in one visit. The public competition record and Replay need no account, API key, trade plan, or model setup.

## Browser tour

1. Open [alphadecay.onrender.com](https://alphadecay.onrender.com). The app checks the published competition record before choosing the first view.

2. If competition history has been published, **Competition Record** opens first. Read the paper `NO_TRADE` decisions or position timeline. A separate account snapshot appears below the timeline when one is available.

3. If no competition history is available, **Explore Demo** opens first. Read **Opening checks**. The production agent freezes an event plan before the signal and calculates direction after the event. Replay uses a fixed sample direction and spread instead of running those steps again.

4. Use **Scenario replay · no setup** to switch between `THESIS_INTACT`, `THETA_TAKEOVER`, `CATALYST_BROKEN`, and `STALE_QUOTE`.

5. Check **Decision**, **Thesis vs. position**, **Agent run**, and **Decision record**. The four samples end in `HOLD`, `ROLL`, `CLOSE`, and `NO_ACTION` respectively. Keep `REPLAY · SAMPLE DATA · NO ORDER SENT` in view.

The Replay examples are synthetic fixtures. Replay does not ask you to supply a plan or direction. It does not call Alpaca, an AI provider, MCP, or the CLI. It cannot place an order, and you do not need to return later to see an outcome.

## Anonymous API path

Set one base URL for the public app.

```bash
BASE_URL=https://alphadecay.onrender.com
```

For a local build, use this instead.

```bash
BASE_URL=http://127.0.0.1:8000
```

Check the service.

```bash
curl -fsS "$BASE_URL/api/health" | jq
```

Run one Replay and keep the useful fields.

```bash
curl -fsS -X POST "$BASE_URL/api/replays/THETA_TAKEOVER" |
  jq '{scenario, action: .assessment.action, rationale: .assessment.rationale_code, quality: .assessment.quality, execution_enabled, input_hash, assessment_hash}'
```

Run all four.

```bash
for scenario in THESIS_INTACT THETA_TAKEOVER CATALYST_BROKEN STALE_QUOTE; do
  curl -fsS -X POST "$BASE_URL/api/replays/$scenario" |
    jq '{scenario, action: .assessment.action, rationale: .assessment.rationale_code, quality: .assessment.quality, execution_enabled}'
done
```

Read the published timeline for the competition paper account.

```bash
curl -fsS "$BASE_URL/api/competition-record" |
  jq '{publication_status, records: [.records[] | {kind, occurred_at, payload}]}'
```

`/api/competition-record` returns published paper `NO_TRADE` decisions and position events in order. When nothing has been published, it returns `NOT_PUBLISHED` with no records. It contains no account or broker identifiers.

Read the separate account performance snapshot.

```bash
curl -fsS "$BASE_URL/api/proof" | jq
```

`/api/proof` reports whether an account performance snapshot was published and whether its measurement is complete, missing, or unknown. It keeps account equity change separate from the lifecycle timeline. A server running Replay mode returns `NOT_PUBLISHED` until the owner deliberately publishes an eligible snapshot.

## Response meanings

- `assessment.action` is the fixed policy result: `HOLD`, `CLOSE`, `ROLL`, or `NO_ACTION`.

- `assessment.rationale_code` is the stable reason code for that result.

- `assessment.quality` shows whether the sample had complete, stale, missing, or unknown decision data.

- `execution_enabled` is always `false` in Replay.

- `input_hash` identifies the exact sample input. `assessment_hash` identifies the policy result.

- `presentation` contains the opening record, later market reading, synthetic evidence, and the list of integrations not run by Replay.

- `certificate` contains the saved thesis, decision, expected exposure after the action, and the record showing that execution was disabled.

- `competition-record.records` contains published history from the competition paper account. `NO_TRADE` is a recorded agent outcome; `POSITION` carries its opening, review, roll, and close events.

- `proof.point` is the separate account performance measurement. It is not the lifecycle record or a score calculated by the organizer.

The route schema lists the four accepted scenario names. Use the [OpenAPI document](https://alphadecay.onrender.com/openapi.json) with tools, or open the [API Reference](https://alphadecay.onrender.com/docs) in a browser. The hosted schema lists only public routes. A connected local copy also documents its protected owner and scheduler routes.

## Scope

Replay is a compact demonstration of the lifecycle policy, not an options scanner or a custom strategy builder. The production runtime starts from an event plan approved by the operator, calculates direction from data observed after the event, and checks an eligible spread with limited risk. Its selected model classifies a bounded evidence set; fixed application policy still chooses the action. Public Replay shows the decision trail immediately with fixed, invented data and does not call that model.
