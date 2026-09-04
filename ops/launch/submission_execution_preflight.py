from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx
from alpaca.trading.client import TradingClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.alpaca.activities import (
    AccountActivitiesAdapter,
    LifecycleAccountActivitiesAdapter,
)
from backend.app.alpaca.execution_evidence import (
    AlpacaExecutionReadCollector,
    AlpacaWholeAccountSweepPort,
)
from backend.app.contracts.v1 import AccountRole
from backend.app.execution import ExecutionAction, ExecutionBlocked, ReconciliationPurpose
from backend.app.persistence.runtime import normalize_database_url, verify_schema
from backend.app.persistence.sqlalchemy_repository import SQLAlchemyExecutionRepository
from backend.app.services.execution import evaluate_broker_mutation_preflight
from ops.launch.opportunity_baseline import (
    _PAPER_ENDPOINT,
    _parse_credentials,
    _read_private_file,
)

_DATABASE_URL_LIMIT = 4096


class SweepPort(Protocol):
    def collect(self, expectation: object) -> object: ...


class _NoGreekReads:
    def collect(self, positions: object) -> tuple[object, ...]:
        del positions
        return ()


def evaluate_submission_execution_preflight(
    repository: SQLAlchemyExecutionRepository,
    *,
    intent_id: UUID | None = None,
    sweep_port: SweepPort | None = None,
) -> dict[str, object]:
    try:
        intent = repository.execution_preflight_intent(AccountRole.SUBMISSION, intent_id)
        if intent.envelope.action is ExecutionAction.ROLL:
            raise ExecutionBlocked("ROLL_EXECUTION_QUOTES_REQUIRED")
        plan = repository.plan_broker_mutation(intent, ReconciliationPurpose.SUBMIT)
        if sweep_port is not None:
            evidence = sweep_port.collect(plan.expectation)
            reconciliation = evaluate_broker_mutation_preflight(
                plan,
                evidence.sweep,
                accepted_at=max(
                    repository.trusted_execution_time(intent),
                    evidence.sweep.retrieval_completed_at,
                ),
            )
            if not reconciliation.safe:
                return {
                    "account_role": AccountRole.SUBMISSION.value,
                    "status": reconciliation.block_codes[0].value,
                }
    except ExecutionBlocked as error:
        return {"account_role": AccountRole.SUBMISSION.value, "status": str(error)}
    return {"account_role": AccountRole.SUBMISSION.value, "status": "READY"}


def resolve_expired_submission_intents(
    repository: SQLAlchemyExecutionRepository, *, persist: bool
) -> dict[str, object]:
    resolved = repository.expired_unsubmitted_claims(AccountRole.SUBMISSION, persist=persist)
    return {
        "account_role": AccountRole.SUBMISSION.value,
        "mode": "PERSISTED" if persist else "PREVIEW",
        "operation": "RESOLVE_EXPIRED_INTENTS",
        "resolved_count": len(resolved),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check the next SUBMISSION broker-write preconditions without mutation"
    )
    parser.add_argument(
        "--role",
        default=AccountRole.SUBMISSION.value,
        choices=(AccountRole.SUBMISSION.value,),
    )
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--database-url-file", required=True, type=Path)
    parser.add_argument("--intent-id", type=UUID)
    parser.add_argument("--resolve-expired-intents", action="store_true")
    parser.add_argument("--persist", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    trading_factory: Callable[..., object] = TradingClient,
    http_factory: Callable[..., object] = httpx.Client,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.persist and not args.resolve_expired_intents:
        parser.error("--persist requires --resolve-expired-intents")
    if not args.resolve_expired_intents and args.credentials_file is None:
        parser.error("--credentials-file is required for execution preflight")
    engine = trading = activity_http = None
    try:
        database_url_bytes = _read_private_file(args.database_url_file)
        if len(database_url_bytes) > _DATABASE_URL_LIMIT:
            database_url_bytes = b""
        database_url = database_url_bytes.decode("utf-8")
        if database_url != database_url.strip() or "\n" in database_url or "\r" in database_url:
            raise ExecutionBlocked("SUBMISSION_EXECUTION_PREFLIGHT_DATABASE_URL_INVALID")
        engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
        verify_schema(engine)
        repository = SQLAlchemyExecutionRepository(sessionmaker(engine, expire_on_commit=False))
        if args.resolve_expired_intents:
            result = resolve_expired_submission_intents(repository, persist=args.persist)
        else:
            result = evaluate_submission_execution_preflight(repository, intent_id=args.intent_id)
            if result["status"] != "READY":
                print(json.dumps(result, sort_keys=True, separators=(",", ":")))
                return 1
            credentials = _parse_credentials(
                json.loads(_read_private_file(args.credentials_file).decode("utf-8"))
            )
            state = repository.get_reconciliation_state(AccountRole.SUBMISSION)
            trading = trading_factory(
                api_key=credentials.api_key,
                secret_key=credentials.secret_key,
                paper=True,
                raw_data=False,
                url_override=_PAPER_ENDPOINT,
            )
            activity_http = http_factory(
                timeout=httpx.Timeout(10.0), follow_redirects=False, trust_env=False
            )
            reads = AlpacaExecutionReadCollector(
                trading,
                account_role=AccountRole.SUBMISSION,
                expected_account_fingerprint=state.account_fingerprint,
                paper=True,
                clock=clock,
            )
            activities = LifecycleAccountActivitiesAdapter(
                AccountActivitiesAdapter(
                    activity_http,
                    base_url=_PAPER_ENDPOINT,
                    api_key=credentials.api_key,
                    secret_key=credentials.secret_key,
                    clock=clock,
                ),
                expected_account_fingerprint=state.account_fingerprint,
            )
            sweep_port = AlpacaWholeAccountSweepPort(
                reads, activities, _NoGreekReads(), repository, clock=clock
            )
            result = evaluate_submission_execution_preflight(
                repository, intent_id=args.intent_id, sweep_port=sweep_port
            )
    except Exception as error:
        parser.error(getattr(error, "code", None) or "SUBMISSION_EXECUTION_PREFLIGHT_FAILED")
    finally:
        for resource in (activity_http, trading):
            close = getattr(resource, "close", None)
            if callable(close):
                close()
        if engine is not None:
            engine.dispose()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status", "READY") == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
