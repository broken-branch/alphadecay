from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from alpaca.trading.client import TradingClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.alpaca.activities import AccountActivitiesAdapter
from backend.app.alpaca.execution_evidence import AlpacaExecutionReadCollector
from backend.app.contracts.v1 import AccountRole
from backend.app.execution import ActivityType, ExecutionBlocked, SweepObservation
from backend.app.persistence.runtime import normalize_database_url, verify_schema
from backend.app.persistence.sqlalchemy_models import SubmissionBaselineRow
from backend.app.persistence.sqlalchemy_repository import SQLAlchemyExecutionRepository
from ops.launch.opportunity_baseline import (
    _PAPER_ENDPOINT,
    _parse_credentials,
    _read_private_file,
)

_DATABASE_URL_LIMIT = 4096


# The paper account is funded before the baseline capture, so the sweep must look back.
_FUNDING_LOOKBACK = timedelta(days=30)


class SubmissionReconciliationInitError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def initialize_submission_reconciliation(
    repository: SQLAlchemyExecutionRepository,
    sweep: SweepObservation,
    *,
    persist: bool,
) -> dict[str, object]:
    state = (
        repository.initialize_reconciliation_state(sweep)
        if persist
        else repository.validate_reconciliation_initialization(sweep)
    )
    return {
        "mode": "PERSISTED" if persist else "PREVIEW",
        "account_role": AccountRole.SUBMISSION.value,
        "sequence": 1,
        "state_hash": state.state_hash,
        "activity_hashes": [item.activity_id_hash for item in state.known_activities],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or seed the SUBMISSION whole-account reconciliation state"
    )
    parser.add_argument(
        "--role",
        default=AccountRole.SUBMISSION.value,
        choices=(AccountRole.SUBMISSION.value,),
    )
    parser.add_argument("--credentials-file", required=True, type=Path)
    parser.add_argument("--database-url-file", required=True, type=Path)
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
    engine = trading = activity_http = None
    try:
        credentials = _parse_credentials(
            json.loads(_read_private_file(args.credentials_file).decode("utf-8"))
        )
        database_url_bytes = _read_private_file(args.database_url_file)
        if len(database_url_bytes) > _DATABASE_URL_LIMIT:
            database_url_bytes = b""
        database_url = database_url_bytes.decode("utf-8")
        if database_url != database_url.strip() or "\n" in database_url or "\r" in database_url:
            raise SubmissionReconciliationInitError(
                "SUBMISSION_RECONCILIATION_DATABASE_URL_INVALID"
            )
        engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
        verify_schema(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        repository = SQLAlchemyExecutionRepository(sessions)
        with sessions() as session:
            baseline = session.scalar(
                select(SubmissionBaselineRow).where(
                    SubmissionBaselineRow.account_role == AccountRole.SUBMISSION.value
                )
            )
        if baseline is None:
            raise SubmissionReconciliationInitError("SUBMISSION_BASELINE_REQUIRED")
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
            expected_account_fingerprint=baseline.account_fingerprint,
            paper=True,
            clock=clock,
        )
        activities = AccountActivitiesAdapter(
            activity_http,
            base_url=_PAPER_ENDPOINT,
            api_key=credentials.api_key,
            secret_key=credentials.secret_key,
            clock=clock,
        )
        started = clock().astimezone(UTC)
        first_account = reads.account()
        first_positions = reads.positions()
        first_orders = reads.open_orders()
        captured_at = baseline.captured_at.astimezone(UTC)
        # The funding journal predates the baseline capture, so it is known state, while the
        # reconciliation window itself must start exactly at the capture and stay empty.
        funding_items, _ = activities.collect(
            since=captured_at - _FUNDING_LOOKBACK,
            until=captured_at,
            provider_to_client={},
        )
        window_items, pagination = activities.collect(
            since=captured_at,
            until=first_account.observed_at,
            provider_to_client={},
        )
        if (
            window_items
            or len(funding_items) != 1
            or funding_items[0].activity_type is not ActivityType.JOURNAL
            or funding_items[0].signed_quantity != baseline.equity
            or funding_items[0].symbol is not None
            or funding_items[0].occurred_at > captured_at
        ):
            raise SubmissionReconciliationInitError("RECONCILIATION_STATE_NOT_CLEAN")
        activity_items = (replace(funding_items[0], activity_type=ActivityType.INITIAL_FUNDING),)
        final_account = reads.account()
        sweep = SweepObservation(
            retrieval_started_at=started,
            retrieval_completed_at=clock().astimezone(UTC),
            activity_pagination=pagination,
            first_account=first_account,
            final_account=final_account,
            first_positions=first_positions,
            final_positions=reads.positions(),
            first_open_orders=first_orders,
            final_open_orders=reads.open_orders(),
            activities=activity_items,
            positions_complete=True,
            orders_complete=True,
        )
        receipt = initialize_submission_reconciliation(repository, sweep, persist=args.persist)
    except Exception as error:
        code = getattr(error, "code", None) or (
            str(error) if isinstance(error, ExecutionBlocked) else None
        )
        parser.error(code or "SUBMISSION_RECONCILIATION_INIT_FAILED")
    finally:
        for resource in (activity_http, trading):
            close = getattr(resource, "close", None)
            if callable(close):
                close()
        if engine is not None:
            engine.dispose()
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
