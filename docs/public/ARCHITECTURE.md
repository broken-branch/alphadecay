# Architecture

AlphaDecay keeps the trade thesis, policy decision, and broker result in one inspectable chain. The browser and API display the chain, but only the authenticated scheduled runtime can reach the paper broker.

```text
plain-English thesis
        |
        v
AI labels supplied evidence --> human reviews fixed rules
        |                              |
        +------------------------------+
                       |
                       v
        saved, hash-bound protocol
                       |
          scheduled market/account check
                       |
                       v
             deterministic policy
              /               \
       NO_TRADE            approved intent
          |                      |
          |               Alpaca paper order
          |                      |
          +----------> broker reconciliation
                                  |
                                  v
                     published decision record
```

The implementation is split across the [strategy brief compiler](../../backend/app/strategy_briefs/protocol.py), [experiment records](../../backend/app/experiments), [policy](../../backend/app/policy), [execution service](../../backend/app/services/agent.py), and [competition archive](../../backend/app/competition_archive). PostgreSQL preserves reviewed definitions, decisions, order attempts, and publications. React renders the public and owner workspaces from the FastAPI service.

## Decision ownership

The model receives named evidence and returns bounded labels such as direction, readiness, and relevance. Application code restores the source details and rejects malformed output. It cannot approve an order. The [curation tests](../../backend/tests/strategy_briefs/test_curation_service.py) cover that boundary.

Fixed code calculates exposure, checks data quality and risk, and chooses the action. Every decision carries hashes for the exact input and result. Repeating the same Replay fixture therefore produces the same certificate; the [Replay integration test](../../backend/tests/integration/test_replay_api.py) checks all four cases.

## Broker boundary

The Trading API adapters read the paper account, clock, bars, option chain, quotes, orders, and positions. Before a write, the service saves one exact intent and stable client order ID. If a response is uncertain, recovery looks up that ID instead of sending the order again. A result becomes final only after the whole paper account matches the expected state. The [agent service tests](../../backend/tests/agent_orchestration/test_agent_run_service.py) exercise entry, recovery, and refusal paths.

The official Alpaca MCP server has a smaller job. The backend launches the pinned executable without a shell and permits only application-selected research tools. Trading and account-changing MCP tools are unavailable. The CLI remains an operator tool outside the web process and production image. The [provider receipt](PROVIDER_REHEARSAL_PROOF.json) and [CLI receipt](CLI_PROOF.json) show their development use.

The scheduler carries no symbol, contract, quantity, or price from its request. It wakes the stored plan, including that plan's bounded quantity, and the service rechecks the account role, market window, evidence age, risk, and order state. A reconciled open book can support another plan evaluation when its controls allow it; each managed position is observed through its own lifecycle while account-wide evidence remains checked. Owner controls can arm or disarm the account gate but cannot widen an intent. These constraints are exercised by the [scheduler authentication tests](../../backend/tests/contracts/test_scheduler_auth.py) and [runtime composition tests](../../backend/tests/runtime_composition).

## Public views

`POST /api/replays/{scenario}` evaluates a fixed fixture without provider access. `GET /api/competition-record` reads only a deliberately published paper lifecycle. `GET /api/proof` returns its separate account checkpoint. The [API guide](AGENT_GUIDE.md) gives one example for each route group.

Replay, development evidence, and the competition account are distinct record types. Public responses remove broker and account identifiers. Protected owner routes use short-lived session and request-protection cookies.

Publication is also a separate step. No-selector owner routes select the newest eligible database record, validate its hashes, and append a sanitized publication. Anonymous requests can only read that projection. The [archive repository tests](../../backend/tests/competition_archive/test_repository.py) cover selection, ordering, and tamper checks.

## Deployment

The [Dockerfile](../../Dockerfile) builds the React assets and Python service into one non-root image. The [Render blueprint](../../render.yaml) declares one web service and private PostgreSQL database. The public deployment runs in Replay-only mode unless its connected server contract is deliberately configured. Health reports the build commit and runtime mode.
