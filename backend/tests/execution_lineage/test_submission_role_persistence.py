from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.alpaca.opportunity import OpportunitySnapshotError, OpportunitySnapshotRequest
from backend.app.contracts.v1 import AccountRole
from backend.app.lifecycle.repository import LifecyclePersistenceError, _require_persisted_role
from backend.app.persistence.opportunity_evidence import (
    OpportunityBaselineSeal,
    OpportunityEvidenceError,
    _executable_role,
    _request_material,
    _validate_submission_baseline,
    opportunity_snapshot_request_from_payload,
)
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    Base,
    SubmissionBaselineRow,
)

ACCOUNT = "a" * 64
CAPTURED_AT = datetime(2026, 8, 28, 15, tzinfo=UTC)


def _seal(baseline_id) -> OpportunityBaselineSeal:
    return OpportunityBaselineSeal(
        plan_id=uuid4(),
        account_fingerprint=ACCOUNT,
        account_source_hash="1" * 64,
        positions_manifest=(),
        positions_source_hash="2" * 64,
        orders_manifest=(),
        orders_source_hash="3" * 64,
        activity_manifest=(),
        activity_source_hash="4" * 64,
        book_hash="5" * 64,
        history_hash="6" * 64,
        captured_at=CAPTURED_AT,
        account_role=AccountRole.SUBMISSION,
        submission_baseline_id=baseline_id,
    )


def _sessions(*, contaminated: bool = False, fingerprint: str = ACCOUNT):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    baseline_id = uuid4()
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.SUBMISSION.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
        session.add(
            SubmissionBaselineRow(
                baseline_id=baseline_id,
                account_role=AccountRole.SUBMISSION.value,
                account_fingerprint=fingerprint,
                equity=Decimal("100000"),
                captured_at=CAPTURED_AT,
                positions_hash="7" * 64,
                orders_hash="8" * 64,
                activities_hash="9" * 64,
                contaminated=contaminated,
            )
        )
    return sessions, baseline_id, engine


def test_submission_opportunity_baseline_requires_clean_exact_submission_baseline() -> None:
    sessions, baseline_id, engine = _sessions()
    with sessions() as session:
        account = session.get(AccountRoleRow, AccountRole.SUBMISSION.value)
        assert account is not None
        _validate_submission_baseline(session, _seal(baseline_id), account)
    engine.dispose()


@pytest.mark.parametrize(
    ("baseline_id", "contaminated", "fingerprint", "code"),
    (
        (None, False, ACCOUNT, "SUBMISSION_BASELINE_REQUIRED"),
        (uuid4(), False, ACCOUNT, "SUBMISSION_BASELINE_REQUIRED"),
        ("existing", True, ACCOUNT, "SUBMISSION_BASELINE_CONTAMINATED"),
        ("existing", False, "b" * 64, "SUBMISSION_BASELINE_MISMATCH"),
    ),
)
def test_submission_opportunity_baseline_rejects_invalid_authority(
    baseline_id, contaminated: bool, fingerprint: str, code: str
) -> None:
    sessions, stored_id, engine = _sessions(
        contaminated=contaminated,
        fingerprint=fingerprint,
    )
    requested_id = stored_id if baseline_id == "existing" else baseline_id
    with sessions() as session:
        account = session.get(AccountRoleRow, AccountRole.SUBMISSION.value)
        assert account is not None
        with pytest.raises(OpportunityEvidenceError, match=code):
            _validate_submission_baseline(session, _seal(requested_id), account)
    engine.dispose()


def test_development_baseline_cannot_bind_submission_baseline() -> None:
    sessions, baseline_id, engine = _sessions()
    with sessions() as session:
        development = AccountRoleRow(
            role=AccountRole.DEVELOPMENT.value,
            account_fingerprint=ACCOUNT,
            equity=Decimal("100000"),
            autonomous_enabled=False,
        )
        with pytest.raises(OpportunityEvidenceError, match="SUBMISSION_BASELINE_FORBIDDEN"):
            _validate_submission_baseline(
                session,
                OpportunityBaselineSeal(
                    **{
                        **_seal(baseline_id).__dict__,
                        "account_role": AccountRole.DEVELOPMENT,
                    }
                ),
                development,
            )
    engine.dispose()


def test_only_executable_roles_are_admitted_by_persistence() -> None:
    for role in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION):
        _executable_role(role)
        _require_persisted_role(role)
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_ROLE_INVALID"):
        _executable_role(AccountRole.REPLAY)
    with pytest.raises(LifecyclePersistenceError, match="EXECUTABLE_ROLE_REQUIRED"):
        _require_persisted_role(AccountRole.REPLAY)


def test_snapshot_request_round_trip_preserves_exact_submission_role() -> None:
    request = OpportunitySnapshotRequest(
        expected_account_fingerprint=ACCOUNT,
        underlying="ACME",
        benchmark="QQQ",
        decision_boundary=datetime(2026, 8, 31, 15, 30, tzinfo=UTC),
        minimum_expiry=datetime(2026, 9, 8, tzinfo=UTC).date(),
        maximum_expiry=datetime(2026, 9, 18, tzinfo=UTC).date(),
        minimum_strike=Decimal("50"),
        maximum_strike=Decimal("150"),
        account_role=AccountRole.SUBMISSION,
    )

    assert opportunity_snapshot_request_from_payload(_request_material(request)) == request
    with pytest.raises(OpportunitySnapshotError, match="OPPORTUNITY_REQUEST_INVALID"):
        OpportunitySnapshotRequest(**{**request.__dict__, "account_role": AccountRole.REPLAY})


def test_forward_migration_keeps_submission_runtime_separately_gated() -> None:
    migration = (
        Path(__file__).parents[3] / "migrations" / "0024_submission_opportunity_authority.sql"
    ).read_text()
    assert "UNIQUE (account_role, opportunity_key, version)" in migration
    assert "submission_baseline_id uuid" in migration
    assert "submission_record.contaminated" in migration
    assert "SUBMISSION_OPPORTUNITY_DECISION_LINEAGE_INVALID" in migration
    assert "NEW.request_contract->>''account_role'' IS DISTINCT FROM NEW.account_role" in migration
    assert "jsonb_object_keys(NEW.request_contract)) <> 12" in migration
    assert "APP_SUBMISSION_OPPORTUNITY_ENABLED" not in migration
    assert "REPLAY" not in migration
