# alphadecay

AlphaDecay freezes the reason for a trade before the position exists, then proves whether each later decision stayed faithful to it.

It turns a plain English options thesis into reviewed rules, records the evidence seen at each scheduled check, and lets fixed policy code decide whether to enter, hold, close, roll, or stand aside. Each frozen plan fixes its own bounded contract quantity. A later entry may be considered against a reconciled open book when that plan permits it, while each managed position keeps its own lifecycle record. The model labels supplied evidence in a fixed format. It does not set risk or choose the action.

## Competition record

> **September 3, 2026 · Alpaca competition paper account** · record: [COMPETITION_RECORD.md](docs/public/COMPETITION_RECORD.md)
>
> **Outcome:** One reconciled SPY call debit spread remains open under lifecycle management.
>
> **Decision:** `ENTRY_APPROVED` at 10:45:01 AM ET after every entry gate passed.
>
> **Primary gate or order state:** Complete single simulated paper fill at 10:45:02 AM ET.
>
> **Broker writes:** One product submitted multileg paper order.
>
> **Account checkpoint:** Whole account reconciliation certified the fill and open managed position.
>
> Every scheduled check since the fill authorized no action, and the position stays open under its plan. The mandatory close is due September 4 at 9:45 AM ET. The record will show the outcome.

The dated record above and [COMPETITION_RECORD.md](docs/public/COMPETITION_RECORD.md) are the published source for the judged trade. The [live demo](https://alphadecay.onrender.com) exposes [`/api/competition-record`](https://alphadecay.onrender.com/api/competition-record) and the account measurement at [`/api/proof`](https://alphadecay.onrender.com/api/proof). Each publishes only what passes its publication check, so `NOT_PUBLISHED` means the check did not run, not that no trade exists. Missing evidence is never shown as a zero or a trade.

## See it in 60 seconds

1. Read the competition record above, then open the [live demo](https://alphadecay.onrender.com). Its **Experiments** view shows a record card only after the publication check has run.
2. Choose **Open Replay**. Select **Time decay takes over** to follow one sample spread from its saved thesis to a `ROLL` decision.
3. Open **Record details** to compare the input and decision hashes, then switch to **The quote is too old to act on** and see the same policy return `NO_ACTION`.

Replay uses fixed sample data, so it is available even when no competition record has been published. Its four expected decisions are pinned by the [Replay API integration test](backend/tests/integration/test_replay_api.py).

## Alpaca proof

The judged account trade is the record above and its dated timeline in [COMPETITION_RECORD.md](docs/public/COMPETITION_RECORD.md): entry approved, the multileg paper order submitted by the product, a complete fill, a reconciliation certificate, and an open managed position. The table below is connection evidence from a development rehearsal, not that trade.

| Alpaca surface | What it does here | Openable proof |
|---|---|---|
| Judged account trade | Records the submitted agent's approved SPY spread, complete simulated paper fill, reconciliation, and managed position. | [Competition Record](https://alphadecay.onrender.com) and [`/api/competition-record`](https://alphadecay.onrender.com/api/competition-record) |
| Trading API | Reads the paper account, market, options, orders, and positions; submits only a stored, approved multileg paper intent; then checks the broker result. | [Development rehearsal receipt](docs/public/PROVIDER_REHEARSAL_PROOF.json) and [execution service tests](backend/tests/agent_orchestration/test_agent_run_service.py) |
| Official MCP server | Supplies application selected, read only research calls to the evidence pipeline. | The receipt's `mcp` section and [MCP boundary tests](backend/tests/provider_evidence/test_mcp_boundary.py) |
| Alpaca CLI | Gives the operator a pinned paper host preview outside the deployed application. | [CLI dry run receipt](docs/public/CLI_PROOF.json) and [bootstrap tests](ops/launch/tests/test_cli_bootstrap.py) |

The development receipt records eleven read only provider requests, one MCP `get_clock` call, the CLI preview, and an unchanged development account book. It remains separate from the judged account trade above.

## Limits

AlphaDecay is an options hackathon prototype with no live trading setting. Alpaca paper fills are simulations, and its free options feed is indicative rather than OPRA. Replay is invented data; development receipts are not judged results; paper performance does not predict live performance. The supported structure is a defined risk vertical spread. If a required quote, account fact, authority check, or broker state is missing or inconsistent, the policy records no action. This is not investment advice. Full evidence boundaries are in the reviewer index.

## Run and test

Requires Python 3.12, uv 0.12.3, Node.js 24.16.0, and npm 11.13.0.

```bash
uv sync --python 3.12 --frozen --all-groups && npm ci && npm run build && uv run --python 3.12 uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

```bash
uv run --python 3.12 pytest -q && npm test -- --run
```

For architecture, API examples, receipts, research, setup, and privacy details, use the single [For reviewers](docs/public/README.md) index.

Released under the [MIT License](LICENSE). Package licenses and retained terms are in [Third-party notices](THIRD_PARTY_NOTICES.md).
