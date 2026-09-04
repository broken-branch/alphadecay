from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.contracts.v1 import (
    AccountRole,
    BaselineStatus,
    CompetitionPerformanceProofResponse,
    MeasurementStatus,
    PerformanceFailureCode,
    PerformancePoint,
)
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    PerformancePublicationRow,
    PerformanceSnapshotRow,
    SubmissionBaselineRow,
)

from .capture import CaptureAuthority
from .models import (
    FINAL_PERFORMANCE_BOUNDARY,
    FINAL_PERFORMANCE_BOUNDARY_KEY,
    FINAL_PUBLICATION_NOT_BEFORE,
    PerformanceSnapshot,
    canonical_hash,
)

HASH_DOMAIN = "alphadecay.performance-publication.v1"
PUBLICATION_LOCK_ID = 6_748_832_220


class NoEligiblePerformanceSnapshot(RuntimeError):
    pass


class PerformanceProofIntegrityError(RuntimeError):
    pass


class PerformanceProofReader(Protocol):
    def latest_publication(self) -> CompetitionPerformanceProofResponse: ...

    def latest_publication_text(self) -> str: ...


class EmptyPerformanceProofReader:
    def latest_publication(self) -> CompetitionPerformanceProofResponse:
        return _unpublished_proof()

    def latest_publication_text(self) -> str:
        return _canonical_json(self.latest_publication().model_dump(mode="json"))


class UnavailablePerformanceProofReader:
    def latest_publication(self) -> CompetitionPerformanceProofResponse:
        raise RuntimeError("performance proof database is unavailable")

    def latest_publication_text(self) -> str:
        raise RuntimeError("performance proof database is unavailable")


class SQLAlchemyPerformanceProofReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def latest_publication(self) -> CompetitionPerformanceProofResponse:
        payload_text = self.latest_publication_text()
        return CompetitionPerformanceProofResponse.model_validate_json(payload_text)

    def latest_publication_text(self) -> str:
        with self._sessions() as session:
            latest, _proof = _latest_verified_publication(session)
            now = _database_now(session)
            if latest is None:
                if now >= FINAL_PERFORMANCE_BOUNDARY:
                    raise PerformanceProofIntegrityError("final performance proof is missing")
                return _canonical_json(_unpublished_proof().model_dump(mode="json"))
            if (
                now >= FINAL_PERFORMANCE_BOUNDARY
                and _utc(latest.boundary_scheduled_for) != FINAL_PERFORMANCE_BOUNDARY
            ):
                raise PerformanceProofIntegrityError("final performance proof is missing")
            return latest.payload_text


class SQLAlchemyPerformanceRepository(SQLAlchemyPerformanceProofReader):
    def trusted_performance_time(self) -> datetime:
        with self._sessions() as session:
            return _database_now(session)

    def performance_capture_authority(self, role: AccountRole) -> CaptureAuthority:
        with self._sessions() as session:
            account = session.get(AccountRoleRow, role.value)
            if account is None:
                raise NoEligiblePerformanceSnapshot(
                    f"{role.value.lower()} account is not registered"
                )
            baseline = session.scalar(
                select(SubmissionBaselineRow).where(
                    SubmissionBaselineRow.account_role == role.value
                )
            )
            if role == AccountRole.DEVELOPMENT:
                if baseline is not None:
                    raise PerformanceProofIntegrityError(
                        "development account has submission baseline authority"
                    )
                return CaptureAuthority(
                    role=role,
                    account_fingerprint=account.account_fingerprint,
                    baseline_id=None,
                    baseline_status=BaselineStatus.NOT_CAPTURED,
                    baseline_equity=None,
                    baseline_captured_at=None,
                )
            if baseline is None:
                return CaptureAuthority(
                    role=role,
                    account_fingerprint=account.account_fingerprint,
                    baseline_id=None,
                    baseline_status=BaselineStatus.NOT_CAPTURED,
                    baseline_equity=None,
                    baseline_captured_at=None,
                )
            if baseline.account_fingerprint != account.account_fingerprint:
                raise PerformanceProofIntegrityError(
                    "submission baseline fingerprint is inconsistent"
                )
            return CaptureAuthority(
                role=role,
                account_fingerprint=account.account_fingerprint,
                baseline_id=baseline.baseline_id,
                baseline_status=(
                    BaselineStatus.CONTAMINATED if baseline.contaminated else BaselineStatus.CLEAN
                ),
                baseline_equity=baseline.equity,
                baseline_captured_at=_utc(baseline.captured_at),
            )

    def append_snapshot(self, snapshot: PerformanceSnapshot) -> None:
        expected = PerformanceSnapshot.create(
            snapshot_id=snapshot.snapshot_id,
            submission_baseline_id=snapshot.submission_baseline_id,
            boundary_key=snapshot.boundary_key,
            point=snapshot.point,
            baseline_status=snapshot.baseline_status,
            account_fingerprint=snapshot.account_fingerprint,
            positions_manifest_hash=snapshot.positions_manifest_hash,
            orders_manifest_hash=snapshot.orders_manifest_hash,
            activities_manifest_hash=snapshot.activities_manifest_hash,
        )
        if expected.snapshot_hash != snapshot.snapshot_hash:
            raise ValueError("snapshot hash does not match its contents")
        try:
            with self._sessions.begin() as session:
                _lock_publication_lane(session)
                _validate_snapshot_authority(session, snapshot)
                by_id = session.get(PerformanceSnapshotRow, snapshot.snapshot_id)
                by_boundary = session.scalar(
                    select(PerformanceSnapshotRow).where(
                        PerformanceSnapshotRow.boundary_key == snapshot.boundary_key
                    )
                )
                existing = by_id or by_boundary
                if existing is not None:
                    if existing.snapshot_hash == snapshot.snapshot_hash:
                        return
                    if by_id is not None:
                        raise ValueError("snapshot already exists with different contents")
                    raise ValueError("boundary already captured with different contents")
                session.add(_snapshot_row(snapshot))
        except IntegrityError as exc:
            raise ValueError("snapshot or boundary already exists") from exc

    def get_snapshot(self, snapshot_id: UUID) -> PerformanceSnapshot | None:
        with self._sessions() as session:
            row = session.get(PerformanceSnapshotRow, snapshot_id)
            return None if row is None else _snapshot_from_row(row)

    def publish_latest_eligible(self) -> CompetitionPerformanceProofResponse:
        with self._sessions.begin() as session:
            _lock_publication_lane(session)
            now = _database_now(session)
            latest, latest_proof = _latest_verified_publication(session)
            snapshot = _select_snapshot(session, now, latest)
            if snapshot is None:
                if latest is None:
                    raise NoEligiblePerformanceSnapshot("no eligible performance snapshot")
                assert latest_proof is not None
                return latest_proof

            _validate_publication_authority(session, snapshot)
            _verify_snapshot_row(snapshot)
            predecessor_hash = None if latest is None else latest.publication_hash
            proof, projection_hash, payload_text = _build_publication(
                snapshot, now, predecessor_hash
            )
            if proof.publication_hash is None:
                raise PerformanceProofIntegrityError("published proof has no hash")
            session.add(
                PerformancePublicationRow(
                    publication_id=uuid5(NAMESPACE_URL, proof.publication_hash),
                    snapshot_id=snapshot.snapshot_id,
                    boundary_scheduled_for=snapshot.scheduled_for,
                    published_at=now,
                    payload_text=payload_text,
                    projection_hash=projection_hash,
                    publication_hash=proof.publication_hash,
                    predecessor_hash=predecessor_hash,
                )
            )
            return proof


def _snapshot_row(snapshot: PerformanceSnapshot) -> PerformanceSnapshotRow:
    point = snapshot.point
    return PerformanceSnapshotRow(
        snapshot_id=snapshot.snapshot_id,
        submission_baseline_id=snapshot.submission_baseline_id,
        account_role="SUBMISSION",
        boundary_key=snapshot.boundary_key,
        scheduled_for=point.scheduled_for,
        attempted_at=point.attempted_at,
        measured_at=point.measured_at,
        measurement_status=point.status.value,
        failure_code=None if point.failure_code is None else point.failure_code.value,
        current_equity=point.current_equity_usd,
        account_equity_change=point.account_equity_change_usd,
        account_equity_return_pct=point.account_equity_return_pct,
        lifecycle_cashflow=point.reconciled_lifecycle_cashflow_usd,
        liquidation_pnl=point.open_position_liquidation_pnl_usd,
        baseline_status=snapshot.baseline_status.value,
        point_payload=point.model_dump(mode="json"),
        account_fingerprint=snapshot.account_fingerprint,
        positions_manifest_hash=snapshot.positions_manifest_hash,
        orders_manifest_hash=snapshot.orders_manifest_hash,
        activities_manifest_hash=snapshot.activities_manifest_hash,
        snapshot_hash=snapshot.snapshot_hash,
    )


def _snapshot_from_row(row: PerformanceSnapshotRow) -> PerformanceSnapshot:
    snapshot = PerformanceSnapshot(
        snapshot_id=row.snapshot_id,
        submission_baseline_id=row.submission_baseline_id,
        boundary_key=row.boundary_key,
        point=PerformancePoint.model_validate(row.point_payload),
        baseline_status=BaselineStatus(row.baseline_status),
        account_fingerprint=row.account_fingerprint,
        positions_manifest_hash=row.positions_manifest_hash,
        orders_manifest_hash=row.orders_manifest_hash,
        activities_manifest_hash=row.activities_manifest_hash,
        snapshot_hash=row.snapshot_hash,
    )
    _verify_snapshot_row(row)
    return snapshot


def _verify_snapshot_row(row: PerformanceSnapshotRow) -> None:
    try:
        point = PerformancePoint.model_validate(row.point_payload)
        snapshot = PerformanceSnapshot.create(
            snapshot_id=row.snapshot_id,
            submission_baseline_id=row.submission_baseline_id,
            boundary_key=row.boundary_key,
            point=point,
            baseline_status=BaselineStatus(row.baseline_status),
            account_fingerprint=row.account_fingerprint,
            positions_manifest_hash=row.positions_manifest_hash,
            orders_manifest_hash=row.orders_manifest_hash,
            activities_manifest_hash=row.activities_manifest_hash,
        )
    except (TypeError, ValueError) as exc:
        raise PerformanceProofIntegrityError("performance snapshot is invalid") from exc
    expected_columns = (
        "SUBMISSION",
        _utc(point.scheduled_for),
        _utc(point.attempted_at),
        None if point.measured_at is None else _utc(point.measured_at),
        point.status.value,
        None if point.failure_code is None else point.failure_code.value,
        point.current_equity_usd,
        point.account_equity_change_usd,
        point.account_equity_return_pct,
        point.reconciled_lifecycle_cashflow_usd,
        point.open_position_liquidation_pnl_usd,
        snapshot.snapshot_hash,
    )
    actual_columns = (
        row.account_role,
        _utc(row.scheduled_for),
        _utc(row.attempted_at),
        None if row.measured_at is None else _utc(row.measured_at),
        row.measurement_status,
        row.failure_code,
        row.current_equity,
        row.account_equity_change,
        row.account_equity_return_pct,
        row.lifecycle_cashflow,
        row.liquidation_pnl,
        row.snapshot_hash,
    )
    if actual_columns != expected_columns:
        raise PerformanceProofIntegrityError("performance snapshot columns do not match payload")


def _validate_snapshot_authority(session: Session, snapshot: PerformanceSnapshot) -> None:
    baseline_id, baseline_status, account_fingerprint = _authoritative_baseline(session)
    if snapshot.account_fingerprint != account_fingerprint:
        raise ValueError("performance snapshot account fingerprint does not match submission")
    if snapshot.submission_baseline_id != baseline_id:
        raise ValueError("performance snapshot baseline does not match submission")
    if snapshot.baseline_status != baseline_status:
        raise ValueError("performance snapshot baseline status is not authoritative")
    if snapshot.point.status == MeasurementStatus.COMPLETE and baseline_id is None:
        raise ValueError("complete performance snapshot requires a sealed baseline")


def _validate_publication_authority(session: Session, snapshot: PerformanceSnapshotRow) -> None:
    account = session.get(AccountRoleRow, "SUBMISSION", with_for_update=True)
    baseline = session.scalar(
        select(SubmissionBaselineRow)
        .where(SubmissionBaselineRow.account_role == "SUBMISSION")
        .with_for_update()
    )
    if (
        account is None
        or baseline is None
        or baseline.account_role != "SUBMISSION"
        or baseline.account_fingerprint != account.account_fingerprint
        or baseline.equity != 100000
        or baseline.contaminated
        or snapshot.account_role != "SUBMISSION"
        or snapshot.account_fingerprint != account.account_fingerprint
        or snapshot.submission_baseline_id != baseline.baseline_id
        or snapshot.baseline_status != BaselineStatus.CLEAN.value
    ):
        raise PerformanceProofIntegrityError(
            "performance publication baseline authority is not clean and current"
        )


def _authoritative_baseline(
    session: Session,
) -> tuple[UUID | None, BaselineStatus, str]:
    account = session.get(AccountRoleRow, "SUBMISSION", with_for_update=True)
    if account is None:
        raise NoEligiblePerformanceSnapshot("submission account is not registered")
    baseline = session.scalar(
        select(SubmissionBaselineRow)
        .where(SubmissionBaselineRow.account_role == "SUBMISSION")
        .with_for_update()
    )
    if baseline is None:
        return None, BaselineStatus.NOT_CAPTURED, account.account_fingerprint
    if baseline.account_fingerprint != account.account_fingerprint:
        raise PerformanceProofIntegrityError("submission baseline fingerprint is inconsistent")
    status = BaselineStatus.CONTAMINATED if baseline.contaminated else BaselineStatus.CLEAN
    return baseline.baseline_id, status, account.account_fingerprint


def _select_snapshot(
    session: Session,
    now: datetime,
    latest: PerformancePublicationRow | None,
) -> PerformanceSnapshotRow | None:
    if now >= FINAL_PERFORMANCE_BOUNDARY:
        if now < FINAL_PUBLICATION_NOT_BEFORE:
            raise NoEligiblePerformanceSnapshot("final capture grace period is active")
        row = session.scalar(
            select(PerformanceSnapshotRow).where(
                PerformanceSnapshotRow.boundary_key == FINAL_PERFORMANCE_BOUNDARY_KEY,
                PerformanceSnapshotRow.scheduled_for == FINAL_PERFORMANCE_BOUNDARY,
            )
        )
        if row is None:
            baseline_id, baseline_status, account_fingerprint = _authoritative_baseline(session)
            point = PerformancePoint(
                scheduled_for=FINAL_PERFORMANCE_BOUNDARY,
                attempted_at=now,
                measured_at=None,
                status=MeasurementStatus.MISSING,
                failure_code=PerformanceFailureCode.CAPTURE_NOT_STARTED,
                current_equity_usd=None,
                account_equity_change_usd=None,
                account_equity_return_pct=None,
                reconciled_lifecycle_cashflow_usd=None,
                open_position_liquidation_pnl_usd=None,
                simulator_limitations_code="ALPACA_PAPER_SIMULATION",
            )
            sentinel = PerformanceSnapshot.create(
                snapshot_id=uuid5(NAMESPACE_URL, FINAL_PERFORMANCE_BOUNDARY_KEY),
                submission_baseline_id=baseline_id,
                boundary_key=FINAL_PERFORMANCE_BOUNDARY_KEY,
                point=point,
                baseline_status=baseline_status,
                account_fingerprint=account_fingerprint,
                positions_manifest_hash=None,
                orders_manifest_hash=None,
                activities_manifest_hash=None,
            )
            row = _snapshot_row(sentinel)
            session.add(row)
            session.flush()
        if latest is not None and latest.snapshot_id == row.snapshot_id:
            return None
        return row
    query = select(PerformanceSnapshotRow).where(PerformanceSnapshotRow.scheduled_for <= now)
    if latest is not None:
        query = query.where(PerformanceSnapshotRow.scheduled_for > latest.boundary_scheduled_for)
    return session.scalar(query.order_by(PerformanceSnapshotRow.scheduled_for.desc()).limit(1))


def _build_publication(
    snapshot: PerformanceSnapshotRow,
    published_at: datetime,
    predecessor_hash: str | None,
) -> tuple[CompetitionPerformanceProofResponse, str, str]:
    projection = {
        "schema_version": "v1",
        "publication_status": "PUBLISHED",
        "baseline_status": snapshot.baseline_status,
        "published_at": _utc_text(published_at),
        "point": snapshot.point_payload,
        "linked_certificate_ids": [],
    }
    projection_hash = canonical_hash(projection)
    publication_hash = _publication_hash(
        snapshot.scheduled_for,
        snapshot.snapshot_hash,
        projection_hash,
        predecessor_hash,
    )
    payload = projection | {
        "publication_hash": publication_hash,
        "predecessor_hash": predecessor_hash,
    }
    proof = CompetitionPerformanceProofResponse.model_validate(payload)
    return proof, projection_hash, _canonical_json(proof.model_dump(mode="json"))


def _verify_publication(
    row: PerformancePublicationRow,
    expected_predecessor: str | None,
    snapshot: PerformanceSnapshotRow,
) -> CompetitionPerformanceProofResponse:
    _verify_snapshot_row(snapshot)
    try:
        payload = json.loads(row.payload_text)
        if _canonical_json(payload) != row.payload_text:
            raise PerformanceProofIntegrityError("publication JSON is not canonical")
        proof = CompetitionPerformanceProofResponse.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise PerformanceProofIntegrityError("publication payload is invalid") from exc
    projection = dict(payload)
    projection.pop("publication_hash", None)
    predecessor = projection.pop("predecessor_hash", None)
    projection_hash = canonical_hash(projection)
    expected_hash = _publication_hash(
        row.boundary_scheduled_for,
        snapshot.snapshot_hash,
        projection_hash,
        expected_predecessor,
    )
    if (
        row.snapshot_id != snapshot.snapshot_id
        or _utc(row.boundary_scheduled_for) != _utc(snapshot.scheduled_for)
        or _utc(row.published_at) != _utc(proof.published_at)
        or proof.point != PerformancePoint.model_validate(snapshot.point_payload)
        or proof.baseline_status != BaselineStatus(snapshot.baseline_status)
        or predecessor != expected_predecessor
        or row.predecessor_hash != expected_predecessor
        or row.projection_hash != projection_hash
        or row.publication_hash != expected_hash
        or proof.publication_hash != expected_hash
    ):
        raise PerformanceProofIntegrityError("publication hash chain is invalid")
    return proof


def _publication_hash(
    scheduled_for: datetime,
    snapshot_hash: str,
    projection_hash: str,
    predecessor_hash: str | None,
) -> str:
    return canonical_hash(
        {
            "domain": HASH_DOMAIN,
            "scheduled_for": _utc_text(_utc(scheduled_for)),
            "snapshot_hash": snapshot_hash,
            "projection_hash": projection_hash,
            "predecessor_hash": predecessor_hash,
        }
    )


def _latest_verified_publication(
    session: Session,
) -> tuple[PerformancePublicationRow | None, CompetitionPerformanceProofResponse | None]:
    rows = tuple(
        session.scalars(
            select(PerformancePublicationRow).order_by(
                PerformancePublicationRow.boundary_scheduled_for,
                PerformancePublicationRow.publication_id,
            )
        )
    )
    predecessor: str | None = None
    proof: CompetitionPerformanceProofResponse | None = None
    for row in rows:
        snapshot = session.get(PerformanceSnapshotRow, row.snapshot_id)
        if snapshot is None:
            raise PerformanceProofIntegrityError("publication snapshot is missing")
        proof = _verify_publication(row, predecessor, snapshot)
        predecessor = proof.publication_hash
    return (None, None) if not rows else (rows[-1], proof)


def _database_now(session: Session) -> datetime:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        value = session.scalar(select(func.clock_timestamp()))
    else:
        value = session.scalar(select(func.current_timestamp()))
    assert isinstance(value, datetime)
    return _utc(value)


def _lock_publication_lane(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": PUBLICATION_LOCK_ID},
        )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _unpublished_proof() -> CompetitionPerformanceProofResponse:
    return CompetitionPerformanceProofResponse(
        publication_status="NOT_PUBLISHED",
        baseline_status=None,
        published_at=None,
        point=None,
        linked_certificate_ids=(),
        publication_hash=None,
        predecessor_hash=None,
    )
