# Reproducibility

AlphaDecay separates four claims that are easy to blur: the code passes its checks, Replay is deterministic, development providers were reachable, and a competition result was published. Each has a different artifact.

## One development receipt

[`PROVIDER_REHEARSAL_PROOF.json`](PROVIDER_REHEARSAL_PROOF.json) records one read-only development rehearsal. It reports eleven provider requests, none that changed the account, one official MCP `get_clock` call, the pinned CLI dry run, and an unchanged account book. The run stopped because it could not identify a managed position.

The receipt includes hashes for its sealed private summary and operator source. Private account and provider records are not published. This proves that the named connections worked during that rehearsal and that the application stopped safely. It does not establish a fill, a competition result, or profit and loss. [`CLI_PROOF.json`](CLI_PROOF.json) contains the CLI portion in a separately inspectable form.

## Replay

Start the app with the command in the root [README](../../README.md), then compare the four fixed scenarios with the [fixture test](../../backend/tests/integration/test_replay_api.py):

```bash
for scenario in THESIS_INTACT THETA_TAKEOVER CATALYST_BROKEN STALE_QUOTE; do
  curl -fsS -X POST "http://127.0.0.1:8000/api/replays/$scenario" |
    jq '{scenario, action: .assessment.action, input_hash, assessment_hash}'
done
```

The expected actions are `HOLD`, `ROLL`, `CLOSE`, and `NO_ACTION`. Check that the browser and API agree on the action, rationale, input hash, assessment hash, current exposure, expected exposure, and rejected alternatives. This verifies the same fixed policy against repository fixtures. Replay is not a provider or market test.

## Repository checks

Install the locked dependencies before running the gate:

```bash
uv sync --python 3.12 --frozen --all-groups
npm ci
uv run --python 3.12 pytest -q
npm test -- --run
npm run typecheck
npm run build
python3 ops/quality/public_copy_check.py
python3 ops/quality/public_link_check.py --root . README.md docs/public/*.md docs/public/reviewers/*.md
```

The backend suite covers policy, persistence, broker recovery, public records, and provider boundaries. Some database tests skip without PostgreSQL; release qualification runs them against a disposable PostgreSQL instance. Browser tests cover the rendered contract. The copy and link checks reject unregistered prose and broken public paths.

Locked dependency and retained-license checks live in [the dependency inventory test](../../ops/release/test_locked_dependency_inventory.py) and [the third-party notices test](../../ops/release/test_third_party_notices.py). A release policy file, kept out of the public tree, defines which files can enter the sanitized repository and blocks private project material.

## Competition result

`GET /api/competition-record` is the published lifecycle source. `GET /api/proof` is the separate account checkpoint. A broker claim requires a terminal paper order with no unresolved remainder and a whole-account match. Lifecycle observation also checks the position against reconciled account evidence; a later eligible entry does not turn that observation into a clean-book assumption. The [competition checkpoint receipt](COMPETITION_CHECKPOINT_PROOF.json) can support the account state without exposing its identifier.

If a route says `NOT_PUBLISHED`, that is the result of the publication check, not evidence of `NO_TRADE`, a fill, or zero return. The root README keeps unresolved outcome fields as named result tokens until the verified record is inserted. Paper results remain simulated and do not predict live performance.
