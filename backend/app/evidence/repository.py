from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from backend.app.contracts.v1 import EvidenceClassification
from backend.app.persistence.sqlalchemy_models import (
    EvidenceClassificationClaimRow,
    EvidenceClassificationRow,
    ModelCallBudgetRow,
)

_MODEL = "gemini-3.7-flash"
_IDENTIFIER_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_EVENT_CODES = frozenset(
    {
        "RESULTS",
        "GUIDANCE",
        "DEMAND",
        "SUPPLY",
        "PRODUCT",
        "CUSTOMER_PARTNER",
        "CAPITAL",
        "REGULATORY_LEGAL",
        "MANAGEMENT",
        "MACRO",
        "OTHER",
    }
)


class EvidenceLedgerError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EvidenceLease:
    evidence_hash: str
    lease_owner: UUID
    generation: int


@dataclass(frozen=True)
class StoredEvidenceClassifications:
    evidence_hash: str
    classifications: tuple[EvidenceClassification, ...]


@dataclass(frozen=True)
class EvidenceClassificationInProgress:
    evidence_hash: str


EvidenceClaim = EvidenceLease | StoredEvidenceClassifications | EvidenceClassificationInProgress


class EvidenceLedger(Protocol):
    def acquire(self, evidence_hash: str) -> EvidenceClaim: ...

    def reserve_model_request(self, model: str) -> int: ...

    def complete(
        self,
        lease: EvidenceLease,
        classifications: tuple[EvidenceClassification, ...],
    ) -> None: ...

    def release(self, lease: EvidenceLease) -> None: ...

    def model_request_count(self, model: str) -> int: ...


class SQLAlchemyEvidenceLedger:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        lease_ttl: timedelta = timedelta(seconds=35),
    ) -> None:
        if not timedelta(0) < lease_ttl <= timedelta(minutes=2):
            raise ValueError("lease_ttl must be positive and at most 2 minutes")
        self._sessions = sessions
        self._lease_ttl = lease_ttl

    def acquire(self, evidence_hash: str) -> EvidenceClaim:
        _validate_hash(evidence_hash)
        with self._sessions.begin() as session:
            stored = session.get(EvidenceClassificationRow, evidence_hash)
            if stored is not None:
                return self._read_stored(session, stored)

            now = _database_now(session)
            lease_owner = uuid4()
            values = {
                "evidence_hash": evidence_hash,
                "state": "PENDING",
                "generation": 1,
                "lease_owner": lease_owner,
                "lease_expires_at": now + self._lease_ttl,
                "updated_at": now,
            }
            insert = _insert_for(session, EvidenceClassificationClaimRow).values(**values)
            inserted = session.execute(
                insert.on_conflict_do_nothing(index_elements=["evidence_hash"]).returning(
                    EvidenceClassificationClaimRow.generation
                )
            ).scalar_one_or_none()
            if inserted is not None:
                return EvidenceLease(evidence_hash, lease_owner, inserted)

            generation = session.execute(
                update(EvidenceClassificationClaimRow)
                .where(
                    EvidenceClassificationClaimRow.evidence_hash == evidence_hash,
                    EvidenceClassificationClaimRow.state == "PENDING",
                    EvidenceClassificationClaimRow.lease_expires_at <= now,
                )
                .values(
                    generation=EvidenceClassificationClaimRow.generation + 1,
                    lease_owner=lease_owner,
                    lease_expires_at=now + self._lease_ttl,
                    updated_at=now,
                )
                .returning(EvidenceClassificationClaimRow.generation)
            ).scalar_one_or_none()
            if generation is not None:
                return EvidenceLease(evidence_hash, lease_owner, generation)

            stored = session.get(EvidenceClassificationRow, evidence_hash)
            if stored is not None:
                return self._read_stored(session, stored)
            claim = session.get(EvidenceClassificationClaimRow, evidence_hash)
            if claim is None or claim.state != "PENDING":
                raise EvidenceLedgerError("MODEL_CLASSIFICATION_INTEGRITY_ERROR")
            return EvidenceClassificationInProgress(evidence_hash)

    def reserve_model_request(self, model: str) -> int:
        if model != _MODEL:
            raise EvidenceLedgerError("MODEL_CALL_BUDGET_MODEL_INVALID")
        with self._sessions.begin() as session:
            request_count = session.execute(
                update(ModelCallBudgetRow)
                .where(
                    ModelCallBudgetRow.model == model,
                    ModelCallBudgetRow.request_count < ModelCallBudgetRow.hard_limit,
                )
                .values(request_count=ModelCallBudgetRow.request_count + 1)
                .returning(ModelCallBudgetRow.request_count)
            ).scalar_one_or_none()
            if request_count is not None:
                return request_count
            budget = session.get(ModelCallBudgetRow, model)
            if budget is None:
                raise EvidenceLedgerError("MODEL_CALL_BUDGET_INTEGRITY_ERROR")
            raise EvidenceLedgerError("MODEL_CALL_BUDGET_EXHAUSTED")

    def complete(
        self,
        lease: EvidenceLease,
        classifications: tuple[EvidenceClassification, ...],
    ) -> None:
        _validate_hash(lease.evidence_hash)
        payload = _classification_payload(classifications)
        classification_hash = _classification_hash(lease.evidence_hash, payload)
        with self._sessions.begin() as session:
            now = _database_now(session)
            claim = session.execute(
                select(EvidenceClassificationClaimRow)
                .where(EvidenceClassificationClaimRow.evidence_hash == lease.evidence_hash)
                .with_for_update()
            ).scalar_one_or_none()
            if not _lease_matches(claim, lease, now):
                raise EvidenceLedgerError("MODEL_CLASSIFICATION_LEASE_LOST")
            session.add(
                EvidenceClassificationRow(
                    evidence_hash=lease.evidence_hash,
                    classifications_payload=payload,
                    classification_hash=classification_hash,
                    completed_generation=lease.generation,
                    completed_at=now,
                )
            )
            session.flush()
            changed = session.execute(
                update(EvidenceClassificationClaimRow)
                .where(
                    EvidenceClassificationClaimRow.evidence_hash == lease.evidence_hash,
                    EvidenceClassificationClaimRow.state == "PENDING",
                    EvidenceClassificationClaimRow.generation == lease.generation,
                    EvidenceClassificationClaimRow.lease_owner == lease.lease_owner,
                    EvidenceClassificationClaimRow.lease_expires_at > now,
                )
                .values(
                    state="COMPLETED",
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if changed != 1:
                raise EvidenceLedgerError("MODEL_CLASSIFICATION_LEASE_LOST")

    def release(self, lease: EvidenceLease) -> None:
        with self._sessions.begin() as session:
            now = _database_now(session)
            session.execute(
                update(EvidenceClassificationClaimRow)
                .where(
                    EvidenceClassificationClaimRow.evidence_hash == lease.evidence_hash,
                    EvidenceClassificationClaimRow.state == "PENDING",
                    EvidenceClassificationClaimRow.generation == lease.generation,
                    EvidenceClassificationClaimRow.lease_owner == lease.lease_owner,
                )
                .values(
                    generation=EvidenceClassificationClaimRow.generation + 1,
                    lease_owner=None,
                    lease_expires_at=now,
                    updated_at=now,
                )
            )

    def model_request_count(self, model: str) -> int:
        if model != _MODEL:
            raise EvidenceLedgerError("MODEL_CALL_BUDGET_MODEL_INVALID")
        with self._sessions() as session:
            budget = session.get(ModelCallBudgetRow, model)
            if budget is None or budget.hard_limit != 50:
                raise EvidenceLedgerError("MODEL_CALL_BUDGET_INTEGRITY_ERROR")
            return budget.request_count

    @staticmethod
    def _read_stored(
        session: Session, stored: EvidenceClassificationRow
    ) -> StoredEvidenceClassifications:
        claim = session.get(EvidenceClassificationClaimRow, stored.evidence_hash)
        if (
            claim is None
            or claim.state != "COMPLETED"
            or claim.generation != stored.completed_generation
        ):
            raise EvidenceLedgerError("MODEL_CLASSIFICATION_INTEGRITY_ERROR")
        try:
            classifications = tuple(
                EvidenceClassification.model_validate(item)
                for item in stored.classifications_payload
            )
            payload = _classification_payload(classifications)
        except (TypeError, ValueError) as exc:
            raise EvidenceLedgerError("MODEL_CLASSIFICATION_INTEGRITY_ERROR") from exc
        if _classification_hash(stored.evidence_hash, payload) != stored.classification_hash:
            raise EvidenceLedgerError("MODEL_CLASSIFICATION_INTEGRITY_ERROR")
        return StoredEvidenceClassifications(stored.evidence_hash, classifications)


def _insert_for(session: Session, model: type[EvidenceClassificationClaimRow]):
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return postgresql_insert(model)
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        return sqlite_insert(model)
    raise EvidenceLedgerError("MODEL_LEDGER_DATABASE_UNSUPPORTED")


def _database_now(session: Session) -> datetime:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        now = session.scalar(select(func.clock_timestamp()))
        if isinstance(now, datetime):
            return _utc(now)
        raise EvidenceLedgerError("MODEL_LEDGER_CLOCK_INVALID")
    return datetime.now(UTC)


def _lease_matches(
    claim: EvidenceClassificationClaimRow | None,
    lease: EvidenceLease,
    now: datetime,
) -> bool:
    if claim is None or claim.lease_expires_at is None:
        return False
    return (
        claim.state == "PENDING"
        and claim.generation == lease.generation
        and claim.lease_owner == lease.lease_owner
        and _utc(claim.lease_expires_at) > now
    )


def _classification_payload(
    classifications: tuple[EvidenceClassification, ...],
) -> list[dict[str, object]]:
    if not 1 <= len(classifications) <= 12:
        raise EvidenceLedgerError("MODEL_CLASSIFICATION_INTEGRITY_ERROR")
    payload: list[dict[str, object]] = []
    cluster_ids: set[str] = set()
    source_ids: set[str] = set()
    for classification in classifications:
        validated = EvidenceClassification.model_validate(classification)
        if validated.event_code not in _EVENT_CODES:
            raise EvidenceLedgerError("MODEL_CLASSIFICATION_INTEGRITY_ERROR")
        _validate_identifier(validated.cluster_id)
        if validated.cluster_id in cluster_ids:
            raise EvidenceLedgerError("MODEL_CLASSIFICATION_INTEGRITY_ERROR")
        cluster_ids.add(validated.cluster_id)
        for source_id in validated.source_ids:
            _validate_identifier(source_id)
            if source_id in source_ids:
                raise EvidenceLedgerError("MODEL_CLASSIFICATION_INTEGRITY_ERROR")
            source_ids.add(source_id)
        if validated.independent_reporting_group is not None:
            _validate_identifier(validated.independent_reporting_group)
        if validated.invalidation_condition_id is not None:
            _validate_identifier(validated.invalidation_condition_id)
        if validated.invalidates != (validated.invalidation_condition_id is not None):
            raise EvidenceLedgerError("MODEL_CLASSIFICATION_INTEGRITY_ERROR")
        payload.append(validated.model_dump(mode="json"))
    return payload


def _classification_hash(evidence_hash: str, payload: list[dict[str, object]]) -> str:
    canonical = json.dumps(
        {"classifications": payload, "evidence_hash": evidence_hash},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_hash(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise EvidenceLedgerError("MODEL_CLASSIFICATION_INTEGRITY_ERROR")


def _validate_identifier(value: str) -> None:
    if not value or len(value) > 160 or any(char not in _IDENTIFIER_CHARACTERS for char in value):
        raise EvidenceLedgerError("MODEL_CLASSIFICATION_INTEGRITY_ERROR")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
