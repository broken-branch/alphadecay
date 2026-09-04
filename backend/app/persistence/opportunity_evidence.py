from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.alpaca.opportunity import (
    OpportunitySnapshotRequest,
    opportunity_snapshot_request_digest,
)
from backend.app.contracts.v1 import AccountRole
from backend.app.policy.opportunity import OpportunityPolicy, opportunity_policy_hash

from .sqlalchemy_models import (
    AccountRoleRow,
    DevelopmentOpportunityBaselineRow,
    DevelopmentOpportunityPlanRow,
    OpportunityObservationManifestRow,
    SubmissionBaselineRow,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_UNDERLYING = re.compile(r"^[A-Z]{1,6}$")


class OpportunityEvidenceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OpportunityPlanSpec:
    opportunity_key: str
    version: int
    underlying: str
    event_session: date
    pre_event_session: date
    reaction_session: date
    signal_session: date
    daily_start_session: date
    allowed_event_codes: tuple[str, ...]
    evidence_window_start: datetime
    evidence_window_end: datetime
    policy: OpportunityPolicy
    request_contract: OpportunitySnapshotRequest
    thesis_code: str
    thesis_target_contract: dict[str, object]
    exposure_limit_contract: dict[str, object]
    invalidation_codes: tuple[str, ...]
    frozen_at: datetime
    account_role: AccountRole = AccountRole.DEVELOPMENT


@dataclass(frozen=True)
class PersistedOpportunityPlan:
    plan_id: UUID
    opportunity_key: str
    version: int
    policy_hash: str
    request_contract_hash: str
    plan_hash: str
    frozen_at: datetime
    account_role: AccountRole = AccountRole.DEVELOPMENT


@dataclass(frozen=True)
class OpportunityBaselineSeal:
    plan_id: UUID
    account_fingerprint: str
    account_source_hash: str
    positions_manifest: tuple[dict[str, object], ...]
    positions_source_hash: str
    orders_manifest: tuple[dict[str, object], ...]
    orders_source_hash: str
    activity_manifest: tuple[dict[str, object], ...]
    activity_source_hash: str
    book_hash: str
    history_hash: str
    captured_at: datetime
    positions_complete: bool = True
    orders_complete: bool = True
    activity_complete: bool = True
    account_role: AccountRole = AccountRole.DEVELOPMENT
    submission_baseline_id: UUID | None = None


@dataclass(frozen=True)
class PersistedOpportunityBaseline:
    baseline_id: UUID
    plan_id: UUID
    account_fingerprint: str
    baseline_hash: str
    captured_at: datetime
    account_role: AccountRole = AccountRole.DEVELOPMENT
    submission_baseline_id: UUID | None = None


@dataclass(frozen=True)
class OpportunityObservationSpec:
    plan_id: UUID
    baseline_id: UUID
    account_fingerprint: str
    policy_hash: str
    request_hash: str
    snapshot_hash: str
    calendar_hash: str
    daily_hash: str
    intraday_hash: str
    signal_authority_hash: str
    halt_hash: str
    catalyst_hash: str
    greek_hash: str
    account_hash: str
    activity_hash: str
    budget_hash: str
    prior_decision_hash: str
    trusted_at: datetime
    evaluated_at: datetime
    account_role: AccountRole = AccountRole.DEVELOPMENT


@dataclass(frozen=True)
class PersistedOpportunityObservation:
    observation_id: UUID
    plan_id: UUID
    baseline_id: UUID
    account_fingerprint: str
    manifest_hash: str
    trusted_at: datetime
    evaluated_at: datetime
    account_role: AccountRole = AccountRole.DEVELOPMENT


@dataclass(frozen=True)
class OpportunityPlanAuthority:
    spec: OpportunityPlanSpec
    persisted: PersistedOpportunityPlan


@dataclass(frozen=True)
class OpportunityBaselineAuthority:
    seal: OpportunityBaselineSeal
    persisted: PersistedOpportunityBaseline


@dataclass(frozen=True)
class OpportunityObservationAuthority:
    spec: OpportunityObservationSpec
    persisted: PersistedOpportunityObservation


class SQLAlchemyOpportunityEvidenceRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def freeze_plan(self, spec: OpportunityPlanSpec) -> PersistedOpportunityPlan:
        (
            material,
            policy_payload,
            request_contract,
            thesis_target_contract,
            exposure_limit_contract,
        ) = _plan_material(spec)
        plan_id, plan_hash = opportunity_plan_identity(spec)
        with self._sessions.begin() as session:
            account = _account(session, spec.account_role)
            if spec.request_contract.expected_account_fingerprint != account.account_fingerprint:
                raise OpportunityEvidenceError(_account_error(spec.account_role, "MISMATCH"))
            existing = session.scalar(
                select(DevelopmentOpportunityPlanRow).where(
                    DevelopmentOpportunityPlanRow.opportunity_key == spec.opportunity_key,
                    DevelopmentOpportunityPlanRow.version == spec.version,
                    DevelopmentOpportunityPlanRow.account_role == spec.account_role.value,
                )
            )
            if existing is not None:
                _verify_plan_row(existing, material, plan_hash, plan_id)
                return _persisted_plan(existing)
            latest = session.scalar(
                select(DevelopmentOpportunityPlanRow)
                .where(
                    DevelopmentOpportunityPlanRow.opportunity_key == spec.opportunity_key,
                    DevelopmentOpportunityPlanRow.account_role == spec.account_role.value,
                )
                .order_by(DevelopmentOpportunityPlanRow.version.desc())
                .limit(1)
            )
            if (latest is None and spec.version != 1) or (
                latest is not None
                and (
                    spec.version != latest.version + 1
                    or spec.frozen_at <= _stored_utc(latest.frozen_at)
                )
            ):
                raise OpportunityEvidenceError("OPPORTUNITY_PLAN_VERSION_INVALID")
            if (
                session.scalar(
                    select(DevelopmentOpportunityPlanRow).where(
                        DevelopmentOpportunityPlanRow.plan_hash == plan_hash
                    )
                )
                is not None
            ):
                raise OpportunityEvidenceError("OPPORTUNITY_PLAN_IDENTITY_CONFLICT")
            row = DevelopmentOpportunityPlanRow(
                plan_id=plan_id,
                opportunity_key=spec.opportunity_key,
                version=spec.version,
                account_role=spec.account_role.value,
                underlying=spec.underlying,
                benchmark_symbol="QQQ",
                event_session=spec.event_session,
                pre_event_session=spec.pre_event_session,
                reaction_session=spec.reaction_session,
                signal_session=spec.signal_session,
                daily_start_session=spec.daily_start_session,
                allowed_event_codes=list(spec.allowed_event_codes),
                evidence_window_start=spec.evidence_window_start,
                evidence_window_end=spec.evidence_window_end,
                policy_payload=policy_payload,
                policy_hash=material["policy_hash"],
                request_contract=request_contract,
                request_contract_hash=material["request_contract_hash"],
                thesis_code=spec.thesis_code,
                thesis_target_contract=thesis_target_contract,
                thesis_target_hash=material["thesis_target_hash"],
                exposure_limit_contract=exposure_limit_contract,
                exposure_limit_hash=material["exposure_limit_hash"],
                invalidation_codes=list(spec.invalidation_codes),
                frozen_at=spec.frozen_at,
                plan_material=material,
                plan_hash=plan_hash,
            )
            session.add(row)
            session.flush()
            return _persisted_plan(row)

    def seal_baseline(self, seal: OpportunityBaselineSeal) -> PersistedOpportunityBaseline:
        material, positions, orders, activity = _baseline_material(seal)
        baseline_id, baseline_hash = opportunity_baseline_identity(seal)
        with self._sessions.begin() as session:
            account = _account(session, seal.account_role)
            plan = session.get(DevelopmentOpportunityPlanRow, seal.plan_id)
            if plan is None:
                raise OpportunityEvidenceError("OPPORTUNITY_PLAN_MISSING")
            if seal.captured_at < _stored_utc(plan.frozen_at):
                raise OpportunityEvidenceError("OPPORTUNITY_BASELINE_CHRONOLOGY_INVALID")
            if plan.account_role != seal.account_role.value:
                raise OpportunityEvidenceError("OPPORTUNITY_ROLE_MISMATCH")
            if account.account_fingerprint != seal.account_fingerprint:
                raise OpportunityEvidenceError(_account_error(seal.account_role, "MISMATCH"))
            _validate_submission_baseline(session, seal, account)
            existing = session.scalar(
                select(DevelopmentOpportunityBaselineRow).where(
                    DevelopmentOpportunityBaselineRow.plan_id == seal.plan_id
                )
            )
            if existing is not None:
                _verify_baseline_row(existing, material, baseline_hash, baseline_id)
                return _persisted_baseline(existing)
            row = DevelopmentOpportunityBaselineRow(
                baseline_id=baseline_id,
                plan_id=seal.plan_id,
                account_role=seal.account_role.value,
                submission_baseline_id=seal.submission_baseline_id,
                account_fingerprint=seal.account_fingerprint,
                account_source_hash=seal.account_source_hash,
                positions_manifest=positions,
                positions_source_hash=seal.positions_source_hash,
                positions_complete=seal.positions_complete,
                orders_manifest=orders,
                orders_source_hash=seal.orders_source_hash,
                orders_complete=seal.orders_complete,
                activity_manifest=activity,
                activity_source_hash=seal.activity_source_hash,
                activity_complete=seal.activity_complete,
                book_hash=seal.book_hash,
                history_hash=seal.history_hash,
                captured_at=seal.captured_at,
                baseline_material=material,
                baseline_hash=baseline_hash,
            )
            session.add(row)
            session.flush()
            return _persisted_baseline(row)

    def append_observation(
        self, spec: OpportunityObservationSpec
    ) -> PersistedOpportunityObservation:
        material = _observation_material(spec)
        manifest_hash = _hash("alphadecay.opportunity.observation.v1", material)
        observation_id = uuid5(NAMESPACE_URL, f"alphadecay:opportunity-observation:{manifest_hash}")
        with self._sessions.begin() as session:
            account = _account(session, spec.account_role)
            plan = session.get(DevelopmentOpportunityPlanRow, spec.plan_id)
            baseline = session.get(DevelopmentOpportunityBaselineRow, spec.baseline_id)
            if plan is None or baseline is None or baseline.plan_id != spec.plan_id:
                raise OpportunityEvidenceError("OPPORTUNITY_OBSERVATION_LINEAGE_INVALID")
            if (
                plan.account_role != spec.account_role.value
                or baseline.account_role != spec.account_role.value
                or spec.account_fingerprint != account.account_fingerprint
                or baseline.account_fingerprint != account.account_fingerprint
            ):
                raise OpportunityEvidenceError(_account_error(spec.account_role, "MISMATCH"))
            if spec.policy_hash != plan.policy_hash:
                raise OpportunityEvidenceError("OPPORTUNITY_POLICY_MISMATCH")
            if spec.trusted_at < _stored_utc(plan.frozen_at) or spec.trusted_at < _stored_utc(
                baseline.captured_at
            ):
                raise OpportunityEvidenceError("OPPORTUNITY_OBSERVATION_CHRONOLOGY_INVALID")
            existing = session.scalar(
                select(OpportunityObservationManifestRow).where(
                    OpportunityObservationManifestRow.plan_id == spec.plan_id
                )
            )
            if existing is not None:
                _verify_observation_row(existing, material, manifest_hash, observation_id)
                return _persisted_observation(existing)
            row = OpportunityObservationManifestRow(
                observation_id=observation_id,
                plan_id=spec.plan_id,
                baseline_id=spec.baseline_id,
                account_role=spec.account_role.value,
                account_fingerprint=spec.account_fingerprint,
                policy_hash=spec.policy_hash,
                request_hash=spec.request_hash,
                snapshot_hash=spec.snapshot_hash,
                calendar_hash=spec.calendar_hash,
                daily_hash=spec.daily_hash,
                intraday_hash=spec.intraday_hash,
                signal_authority_hash=spec.signal_authority_hash,
                halt_hash=spec.halt_hash,
                catalyst_hash=spec.catalyst_hash,
                greek_hash=spec.greek_hash,
                account_hash=spec.account_hash,
                activity_hash=spec.activity_hash,
                budget_hash=spec.budget_hash,
                prior_decision_hash=spec.prior_decision_hash,
                trusted_at=spec.trusted_at,
                evaluated_at=spec.evaluated_at,
                observation_material=material,
                manifest_hash=manifest_hash,
            )
            session.add(row)
            session.flush()
            return _persisted_observation(row)

    def load_plan(
        self,
        opportunity_key: str,
        *,
        version: int | None = None,
        account_role: AccountRole = AccountRole.DEVELOPMENT,
    ) -> OpportunityPlanAuthority | None:
        if not _matches(_KEY, opportunity_key) or (
            version is not None
            and (not isinstance(version, int) or isinstance(version, bool) or version <= 0)
        ):
            raise OpportunityEvidenceError("OPPORTUNITY_PLAN_LOOKUP_INVALID")
        with self._sessions() as session:
            account = _read_account(session, account_role)
            query = select(DevelopmentOpportunityPlanRow).where(
                DevelopmentOpportunityPlanRow.opportunity_key == opportunity_key,
                DevelopmentOpportunityPlanRow.account_role == account_role.value,
            )
            if version is None:
                query = query.order_by(DevelopmentOpportunityPlanRow.version.desc()).limit(1)
            else:
                query = query.where(DevelopmentOpportunityPlanRow.version == version)
            row = session.scalar(query)
            if row is None:
                return None
            return _load_plan_authority(row, account)

    def load_baseline(
        self, plan_id: UUID, *, account_role: AccountRole = AccountRole.DEVELOPMENT
    ) -> OpportunityBaselineAuthority | None:
        if not isinstance(plan_id, UUID):
            raise OpportunityEvidenceError("OPPORTUNITY_BASELINE_LOOKUP_INVALID")
        with self._sessions() as session:
            account = _read_account(session, account_role)
            plan = session.get(DevelopmentOpportunityPlanRow, plan_id)
            if plan is None:
                return None
            _load_plan_authority(plan, account)
            row = session.scalar(
                select(DevelopmentOpportunityBaselineRow).where(
                    DevelopmentOpportunityBaselineRow.plan_id == plan_id
                )
            )
            if row is None:
                return None
            return _load_baseline_authority(session, row, plan, account)

    def load_observation(
        self, plan_id: UUID, *, account_role: AccountRole = AccountRole.DEVELOPMENT
    ) -> OpportunityObservationAuthority | None:
        if not isinstance(plan_id, UUID):
            raise OpportunityEvidenceError("OPPORTUNITY_OBSERVATION_LOOKUP_INVALID")
        with self._sessions() as session:
            account = _read_account(session, account_role)
            plan = session.get(DevelopmentOpportunityPlanRow, plan_id)
            if plan is None:
                return None
            _load_plan_authority(plan, account)
            baseline = session.scalar(
                select(DevelopmentOpportunityBaselineRow).where(
                    DevelopmentOpportunityBaselineRow.plan_id == plan_id
                )
            )
            if baseline is None:
                return None
            _load_baseline_authority(session, baseline, plan, account)
            row = session.scalar(
                select(OpportunityObservationManifestRow).where(
                    OpportunityObservationManifestRow.plan_id == plan_id
                )
            )
            if row is None:
                return None
            return _load_observation_authority(row, plan, baseline, account)


def opportunity_plan_digest(spec: OpportunityPlanSpec) -> str:
    material, _, _, _, _ = _plan_material(spec)
    return _hash("alphadecay.opportunity.plan.v1", material)


def opportunity_plan_identity(spec: OpportunityPlanSpec) -> tuple[UUID, str]:
    digest = opportunity_plan_digest(spec)
    return uuid5(NAMESPACE_URL, f"alphadecay:opportunity-plan:{digest}"), digest


def opportunity_baseline_digest(seal: OpportunityBaselineSeal) -> str:
    material, _, _, _ = _baseline_material(seal)
    return _hash("alphadecay.opportunity.baseline.v1", material)


def opportunity_baseline_identity(seal: OpportunityBaselineSeal) -> tuple[UUID, str]:
    digest = opportunity_baseline_digest(seal)
    return uuid5(NAMESPACE_URL, f"alphadecay:opportunity-baseline:{digest}"), digest


def opportunity_observation_digest(spec: OpportunityObservationSpec) -> str:
    return _hash("alphadecay.opportunity.observation.v1", _observation_material(spec))


def _load_plan_authority(
    row: DevelopmentOpportunityPlanRow, account: AccountRoleRow
) -> OpportunityPlanAuthority:
    try:
        spec = OpportunityPlanSpec(
            opportunity_key=row.opportunity_key,
            version=row.version,
            underlying=row.underlying,
            event_session=row.event_session,
            pre_event_session=row.pre_event_session,
            reaction_session=row.reaction_session,
            signal_session=row.signal_session,
            daily_start_session=row.daily_start_session,
            allowed_event_codes=_code_tuple(row.allowed_event_codes),
            evidence_window_start=_stored_utc(row.evidence_window_start),
            evidence_window_end=_stored_utc(row.evidence_window_end),
            policy=opportunity_policy_from_payload(row.policy_payload),
            request_contract=opportunity_snapshot_request_from_payload(row.request_contract),
            thesis_code=row.thesis_code,
            thesis_target_contract=_document(row.thesis_target_contract),
            exposure_limit_contract=_document(row.exposure_limit_contract),
            invalidation_codes=_code_tuple(row.invalidation_codes),
            frozen_at=_stored_utc(row.frozen_at),
            account_role=AccountRole(row.account_role),
        )
        material, _, _, _, _ = _plan_material(spec)
        plan_hash = _hash("alphadecay.opportunity.plan.v1", material)
        plan_id = uuid5(NAMESPACE_URL, f"alphadecay:opportunity-plan:{plan_hash}")
        _verify_plan_row(row, material, plan_hash, plan_id)
    except OpportunityEvidenceError as error:
        if error.code == "OPPORTUNITY_PLAN_VERSION_CONFLICT":
            raise
        raise OpportunityEvidenceError("OPPORTUNITY_PLAN_VERSION_CONFLICT") from error
    except (TypeError, ValueError, OverflowError) as error:
        raise OpportunityEvidenceError("OPPORTUNITY_PLAN_VERSION_CONFLICT") from error
    if (
        row.account_role != account.role
        or spec.request_contract.expected_account_fingerprint != account.account_fingerprint
    ):
        raise OpportunityEvidenceError(_account_error(AccountRole(account.role), "MISMATCH"))
    return OpportunityPlanAuthority(spec=spec, persisted=_persisted_plan(row))


def _load_baseline_authority(
    session: Session,
    row: DevelopmentOpportunityBaselineRow,
    plan: DevelopmentOpportunityPlanRow,
    account: AccountRoleRow,
) -> OpportunityBaselineAuthority:
    try:
        seal = OpportunityBaselineSeal(
            plan_id=row.plan_id,
            account_fingerprint=row.account_fingerprint,
            account_source_hash=row.account_source_hash,
            positions_manifest=_manifest_tuple(row.positions_manifest),
            positions_source_hash=row.positions_source_hash,
            positions_complete=row.positions_complete,
            orders_manifest=_manifest_tuple(row.orders_manifest),
            orders_source_hash=row.orders_source_hash,
            orders_complete=row.orders_complete,
            activity_manifest=_manifest_tuple(row.activity_manifest),
            activity_source_hash=row.activity_source_hash,
            activity_complete=row.activity_complete,
            book_hash=row.book_hash,
            history_hash=row.history_hash,
            captured_at=_stored_utc(row.captured_at),
            account_role=AccountRole(row.account_role),
            submission_baseline_id=row.submission_baseline_id,
        )
        material, _, _, _ = _baseline_material(seal)
        baseline_hash = _hash("alphadecay.opportunity.baseline.v1", material)
        baseline_id = uuid5(NAMESPACE_URL, f"alphadecay:opportunity-baseline:{baseline_hash}")
        _verify_baseline_row(row, material, baseline_hash, baseline_id)
    except OpportunityEvidenceError as error:
        if error.code == "OPPORTUNITY_BASELINE_CONFLICT":
            raise
        raise OpportunityEvidenceError("OPPORTUNITY_BASELINE_CONFLICT") from error
    except (TypeError, ValueError, OverflowError) as error:
        raise OpportunityEvidenceError("OPPORTUNITY_BASELINE_CONFLICT") from error
    if row.plan_id != plan.plan_id:
        raise OpportunityEvidenceError("OPPORTUNITY_OBSERVATION_LINEAGE_INVALID")
    if (
        row.account_role != account.role
        or row.account_role != plan.account_role
        or row.account_fingerprint != account.account_fingerprint
    ):
        raise OpportunityEvidenceError(_account_error(AccountRole(account.role), "MISMATCH"))
    _validate_submission_baseline(session, seal, account)
    if seal.captured_at < _stored_utc(plan.frozen_at):
        raise OpportunityEvidenceError("OPPORTUNITY_BASELINE_CHRONOLOGY_INVALID")
    return OpportunityBaselineAuthority(seal=seal, persisted=_persisted_baseline(row))


def _load_observation_authority(
    row: OpportunityObservationManifestRow,
    plan: DevelopmentOpportunityPlanRow,
    baseline: DevelopmentOpportunityBaselineRow,
    account: AccountRoleRow,
) -> OpportunityObservationAuthority:
    try:
        spec = OpportunityObservationSpec(
            plan_id=row.plan_id,
            baseline_id=row.baseline_id,
            account_fingerprint=row.account_fingerprint,
            policy_hash=row.policy_hash,
            request_hash=row.request_hash,
            snapshot_hash=row.snapshot_hash,
            calendar_hash=row.calendar_hash,
            daily_hash=row.daily_hash,
            intraday_hash=row.intraday_hash,
            signal_authority_hash=row.signal_authority_hash,
            halt_hash=row.halt_hash,
            catalyst_hash=row.catalyst_hash,
            greek_hash=row.greek_hash,
            account_hash=row.account_hash,
            activity_hash=row.activity_hash,
            budget_hash=row.budget_hash,
            prior_decision_hash=row.prior_decision_hash,
            trusted_at=_stored_utc(row.trusted_at),
            evaluated_at=_stored_utc(row.evaluated_at),
            account_role=AccountRole(row.account_role),
        )
        material = _observation_material(spec)
        manifest_hash = _hash("alphadecay.opportunity.observation.v1", material)
        observation_id = uuid5(NAMESPACE_URL, f"alphadecay:opportunity-observation:{manifest_hash}")
        _verify_observation_row(row, material, manifest_hash, observation_id)
    except OpportunityEvidenceError as error:
        if error.code in {
            "OPPORTUNITY_OBSERVATION_CONFLICT",
            "OPPORTUNITY_OBSERVATION_CHRONOLOGY_INVALID",
        }:
            raise
        raise OpportunityEvidenceError("OPPORTUNITY_OBSERVATION_CONFLICT") from error
    except (TypeError, ValueError, OverflowError) as error:
        raise OpportunityEvidenceError("OPPORTUNITY_OBSERVATION_CONFLICT") from error
    if row.plan_id != plan.plan_id or row.baseline_id != baseline.baseline_id:
        raise OpportunityEvidenceError("OPPORTUNITY_OBSERVATION_LINEAGE_INVALID")
    if (
        row.account_role != account.role
        or row.account_role != plan.account_role
        or row.account_role != baseline.account_role
        or row.account_fingerprint != account.account_fingerprint
        or baseline.account_fingerprint != account.account_fingerprint
    ):
        raise OpportunityEvidenceError(_account_error(AccountRole(account.role), "MISMATCH"))
    if row.policy_hash != plan.policy_hash:
        raise OpportunityEvidenceError("OPPORTUNITY_POLICY_MISMATCH")
    if spec.trusted_at < max(_stored_utc(plan.frozen_at), _stored_utc(baseline.captured_at)):
        raise OpportunityEvidenceError("OPPORTUNITY_OBSERVATION_CHRONOLOGY_INVALID")
    return OpportunityObservationAuthority(spec=spec, persisted=_persisted_observation(row))


def _plan_material(
    spec: OpportunityPlanSpec,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    frozen_at = _utc(spec.frozen_at, "OPPORTUNITY_PLAN_TIME_INVALID")
    evidence_window_start = _utc(spec.evidence_window_start, "OPPORTUNITY_PLAN_TIME_INVALID")
    evidence_window_end = _utc(spec.evidence_window_end, "OPPORTUNITY_PLAN_TIME_INVALID")
    if (
        not _matches(_KEY, spec.opportunity_key)
        or not isinstance(spec.version, int)
        or isinstance(spec.version, bool)
        or spec.version <= 0
        or not _matches(_UNDERLYING, spec.underlying)
        or type(spec.event_session) is not date
        or type(spec.pre_event_session) is not date
        or type(spec.reaction_session) is not date
        or type(spec.signal_session) is not date
        or type(spec.daily_start_session) is not date
        or type(spec.allowed_event_codes) is not tuple
        or not 1 <= len(spec.allowed_event_codes) <= 12
        or len(set(spec.allowed_event_codes)) != len(spec.allowed_event_codes)
        or any(not _matches(_CODE, code) for code in spec.allowed_event_codes)
        or spec.daily_start_session.weekday() >= 5
        or not 1 <= (spec.signal_session - spec.daily_start_session).days <= 120
        or not isinstance(spec.policy, OpportunityPolicy)
        or not isinstance(spec.request_contract, OpportunitySnapshotRequest)
        or spec.account_role not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}
        or spec.request_contract.account_role is not spec.account_role
        or spec.request_contract.underlying != spec.underlying
        or spec.request_contract.benchmark != "QQQ"
        or spec.request_contract.decision_boundary != spec.policy.selected_decision_boundary
        or spec.opportunity_key != spec.policy.opportunity_key
        or spec.underlying != spec.policy.underlying
        or spec.policy.selected_decision_boundary.date() != spec.signal_session
        or not _matches(_CODE, spec.thesis_code)
        or not spec.invalidation_codes
        or len(set(spec.invalidation_codes)) != len(spec.invalidation_codes)
        or any(not _matches(_CODE, code) for code in spec.invalidation_codes)
        or not (
            spec.daily_start_session
            < spec.pre_event_session
            < spec.event_session
            < spec.reaction_session
            <= spec.signal_session
        )
        or not frozen_at
        <= evidence_window_start
        <= evidence_window_end
        <= spec.policy.selected_decision_boundary
    ):
        raise OpportunityEvidenceError("OPPORTUNITY_PLAN_INVALID")
    contracts = (spec.thesis_target_contract, spec.exposure_limit_contract)
    if any(not isinstance(contract, dict) or not contract for contract in contracts):
        raise OpportunityEvidenceError("OPPORTUNITY_PLAN_CONTRACT_INVALID")
    policy_payload = _json_value(spec.policy)
    assert isinstance(policy_payload, dict)
    policy_hash = opportunity_policy_hash(spec.policy)
    if policy_hash != _policy_payload_hash(policy_payload):
        raise OpportunityEvidenceError("OPPORTUNITY_PLAN_POLICY_INVALID")
    request_contract = _request_material(spec.request_contract)
    thesis_target_contract = _document(spec.thesis_target_contract)
    exposure_limit_contract = _document(spec.exposure_limit_contract)
    material: dict[str, object] = {
        "account_role": spec.account_role.value,
        "opportunity_key": spec.opportunity_key,
        "version": spec.version,
        "underlying": spec.underlying,
        "benchmark_symbol": "QQQ",
        "event_session": spec.event_session.isoformat(),
        "pre_event_session": spec.pre_event_session.isoformat(),
        "reaction_session": spec.reaction_session.isoformat(),
        "signal_session": spec.signal_session.isoformat(),
        "daily_start_session": spec.daily_start_session.isoformat(),
        "allowed_event_codes": list(spec.allowed_event_codes),
        "evidence_window_start": evidence_window_start.isoformat(),
        "evidence_window_end": evidence_window_end.isoformat(),
        "policy_payload": policy_payload,
        "policy_hash": policy_hash,
        "request_contract": request_contract,
        "request_contract_hash": opportunity_snapshot_request_digest(spec.request_contract),
        "thesis_code": spec.thesis_code,
        "thesis_target_contract": thesis_target_contract,
        "thesis_target_hash": _hash(
            "alphadecay.opportunity.thesis-target.v1", thesis_target_contract
        ),
        "exposure_limit_contract": exposure_limit_contract,
        "exposure_limit_hash": _hash(
            "alphadecay.opportunity.exposure-limit.v1", exposure_limit_contract
        ),
        "invalidation_codes": list(spec.invalidation_codes),
        "frozen_at": frozen_at.isoformat(),
    }
    return (
        material,
        policy_payload,
        request_contract,
        thesis_target_contract,
        exposure_limit_contract,
    )


def _baseline_material(
    seal: OpportunityBaselineSeal,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    captured_at = _utc(seal.captured_at, "OPPORTUNITY_BASELINE_TIME_INVALID")
    _executable_role(seal.account_role)
    if (
        seal.account_role is AccountRole.DEVELOPMENT and seal.submission_baseline_id is not None
    ) or (
        seal.account_role is AccountRole.SUBMISSION
        and not isinstance(seal.submission_baseline_id, UUID)
    ):
        raise OpportunityEvidenceError("OPPORTUNITY_BASELINE_ROLE_BINDING_INVALID")
    hashes = (
        seal.account_fingerprint,
        seal.account_source_hash,
        seal.positions_source_hash,
        seal.orders_source_hash,
        seal.activity_source_hash,
        seal.book_hash,
        seal.history_hash,
    )
    if not isinstance(seal.plan_id, UUID) or any(not _matches(_HASH, value) for value in hashes):
        raise OpportunityEvidenceError("OPPORTUNITY_BASELINE_HASH_INVALID")
    if not (seal.positions_complete and seal.orders_complete and seal.activity_complete):
        raise OpportunityEvidenceError("OPPORTUNITY_BASELINE_INCOMPLETE")
    manifests = (seal.positions_manifest, seal.orders_manifest, seal.activity_manifest)
    if any(
        not isinstance(manifest, tuple) or any(not isinstance(item, dict) for item in manifest)
        for manifest in manifests
    ):
        raise OpportunityEvidenceError("OPPORTUNITY_BASELINE_MANIFEST_INVALID")
    positions = _manifest(seal.positions_manifest)
    orders = _manifest(seal.orders_manifest)
    activity = _manifest(seal.activity_manifest)
    material: dict[str, object] = {
        "plan_id": str(seal.plan_id),
        "account_role": seal.account_role.value,
        "account_fingerprint": seal.account_fingerprint,
        "account_source_hash": seal.account_source_hash,
        "positions_manifest": positions,
        "positions_source_hash": seal.positions_source_hash,
        "positions_complete": True,
        "orders_manifest": orders,
        "orders_source_hash": seal.orders_source_hash,
        "orders_complete": True,
        "activity_manifest": activity,
        "activity_source_hash": seal.activity_source_hash,
        "activity_complete": True,
        "book_hash": seal.book_hash,
        "history_hash": seal.history_hash,
        "captured_at": captured_at.isoformat(),
    }
    if seal.account_role is AccountRole.SUBMISSION:
        material["submission_baseline_id"] = str(seal.submission_baseline_id)
    return material, positions, orders, activity


def _observation_material(spec: OpportunityObservationSpec) -> dict[str, object]:
    trusted_at = _utc(spec.trusted_at, "OPPORTUNITY_TRUSTED_TIME_INVALID")
    evaluated_at = _utc(spec.evaluated_at, "OPPORTUNITY_EVALUATED_TIME_INVALID")
    _executable_role(spec.account_role)
    if evaluated_at < trusted_at:
        raise OpportunityEvidenceError("OPPORTUNITY_OBSERVATION_CHRONOLOGY_INVALID")
    hashes = (
        spec.account_fingerprint,
        spec.policy_hash,
        spec.request_hash,
        spec.snapshot_hash,
        spec.calendar_hash,
        spec.daily_hash,
        spec.intraday_hash,
        spec.signal_authority_hash,
        spec.halt_hash,
        spec.catalyst_hash,
        spec.greek_hash,
        spec.account_hash,
        spec.activity_hash,
        spec.budget_hash,
        spec.prior_decision_hash,
    )
    if (
        not isinstance(spec.plan_id, UUID)
        or not isinstance(spec.baseline_id, UUID)
        or any(not _matches(_HASH, value) for value in hashes)
    ):
        raise OpportunityEvidenceError("OPPORTUNITY_OBSERVATION_HASH_INVALID")
    return {
        "plan_id": str(spec.plan_id),
        "baseline_id": str(spec.baseline_id),
        "account_role": spec.account_role.value,
        "account_fingerprint": spec.account_fingerprint,
        "policy_hash": spec.policy_hash,
        "request_hash": spec.request_hash,
        "snapshot_hash": spec.snapshot_hash,
        "calendar_hash": spec.calendar_hash,
        "daily_hash": spec.daily_hash,
        "intraday_hash": spec.intraday_hash,
        "signal_authority_hash": spec.signal_authority_hash,
        "halt_hash": spec.halt_hash,
        "catalyst_hash": spec.catalyst_hash,
        "greek_hash": spec.greek_hash,
        "account_hash": spec.account_hash,
        "activity_hash": spec.activity_hash,
        "budget_hash": spec.budget_hash,
        "prior_decision_hash": spec.prior_decision_hash,
        "trusted_at": trusted_at.isoformat(),
        "evaluated_at": evaluated_at.isoformat(),
    }


def _account(session: Session, role: AccountRole) -> AccountRoleRow:
    _executable_role(role)
    account = session.scalar(
        select(AccountRoleRow).where(AccountRoleRow.role == role.value).with_for_update()
    )
    if account is None:
        raise OpportunityEvidenceError("OPPORTUNITY_ACCOUNT_MISSING")
    if not _matches(_HASH, account.account_fingerprint):
        raise OpportunityEvidenceError("OPPORTUNITY_ACCOUNT_INVALID")
    return account


def _read_account(session: Session, role: AccountRole) -> AccountRoleRow:
    _executable_role(role)
    account = session.scalar(select(AccountRoleRow).where(AccountRoleRow.role == role.value))
    if account is None:
        raise OpportunityEvidenceError("OPPORTUNITY_ACCOUNT_MISSING")
    if not _matches(_HASH, account.account_fingerprint):
        raise OpportunityEvidenceError("OPPORTUNITY_ACCOUNT_INVALID")
    return account


def _executable_role(role: AccountRole) -> None:
    if role not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}:
        raise OpportunityEvidenceError("OPPORTUNITY_ROLE_INVALID")


def _account_error(role: AccountRole, suffix: str) -> str:
    prefix = "DEVELOPMENT_ACCOUNT" if role is AccountRole.DEVELOPMENT else "SUBMISSION_ACCOUNT"
    return f"{prefix}_{suffix}"


def _validate_submission_baseline(
    session: Session,
    seal: OpportunityBaselineSeal,
    account: AccountRoleRow,
) -> None:
    if seal.account_role is AccountRole.DEVELOPMENT:
        if seal.submission_baseline_id is not None:
            raise OpportunityEvidenceError("SUBMISSION_BASELINE_FORBIDDEN")
        return
    if seal.account_role is not AccountRole.SUBMISSION or seal.submission_baseline_id is None:
        raise OpportunityEvidenceError("SUBMISSION_BASELINE_REQUIRED")
    baseline = session.get(SubmissionBaselineRow, seal.submission_baseline_id)
    if baseline is None:
        raise OpportunityEvidenceError("SUBMISSION_BASELINE_REQUIRED")
    if baseline.contaminated:
        raise OpportunityEvidenceError("SUBMISSION_BASELINE_CONTAMINATED")
    if (
        baseline.account_role != AccountRole.SUBMISSION.value
        or baseline.account_fingerprint != account.account_fingerprint
        or baseline.account_fingerprint != seal.account_fingerprint
    ):
        raise OpportunityEvidenceError("SUBMISSION_BASELINE_MISMATCH")


def _utc(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise OpportunityEvidenceError(code)
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _document(value: dict[str, object]) -> dict[str, object]:
    try:
        normalized = _json_value(value)
    except (TypeError, ValueError) as error:
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID") from error
    if not isinstance(normalized, dict):
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    return normalized


def _manifest(value: tuple[dict[str, object], ...]) -> list[dict[str, object]]:
    try:
        normalized = _json_value(value)
    except (TypeError, ValueError) as error:
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID") from error
    if not isinstance(normalized, list) or any(not isinstance(item, dict) for item in normalized):
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    return normalized


def _manifest_tuple(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    return tuple(_document(item) for item in value)


def _code_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    return tuple(value)


def opportunity_policy_from_payload(value: object) -> OpportunityPolicy:
    payload = _mapping(value)
    try:
        return OpportunityPolicy(
            version=_string(payload, "version"),
            opportunity_key=_string(payload, "opportunity_key"),
            underlying=_string(payload, "underlying"),
            selected_decision_boundary=_datetime(payload, "selected_decision_boundary"),
            last_entry_boundary=_datetime(payload, "last_entry_boundary"),
            maximum_decision_delay=_duration(payload, "maximum_decision_delay"),
            maximum_underlying_age=_duration(payload, "maximum_underlying_age"),
            maximum_catalyst_age=_duration(payload, "maximum_catalyst_age"),
            maximum_option_quote_age=_duration(payload, "maximum_option_quote_age"),
            maximum_leg_quote_skew=_duration(payload, "maximum_leg_quote_skew"),
            minimum_vwap_distance=_decimal(payload, "minimum_vwap_distance"),
            maximum_vwap_distance=_decimal(payload, "maximum_vwap_distance"),
            minimum_relative_return=_decimal(payload, "minimum_relative_return"),
            minimum_beta=_decimal(payload, "minimum_beta"),
            maximum_beta=_decimal(payload, "maximum_beta"),
            required_trend_hits=_integer(payload, "required_trend_hits"),
            maximum_first_reaction=_decimal(payload, "maximum_first_reaction"),
            minimum_catalyst_score=_integer(payload, "minimum_catalyst_score"),
            minimum_candidate_score=_integer(payload, "minimum_candidate_score"),
            minimum_dte=_integer(payload, "minimum_dte"),
            maximum_dte=_integer(payload, "maximum_dte"),
            maximum_relative_spread=_decimal(payload, "maximum_relative_spread"),
            minimum_debit_width_fraction=_decimal(payload, "minimum_debit_width_fraction"),
            maximum_debit_width_fraction=_decimal(payload, "maximum_debit_width_fraction"),
            minimum_credit_width_fraction=_decimal(payload, "minimum_credit_width_fraction"),
            maximum_position_loss=_decimal(payload, "maximum_position_loss"),
            maximum_equity_risk_fraction=_decimal(payload, "maximum_equity_risk_fraction"),
            maximum_lifetime_entries=_integer(payload, "maximum_lifetime_entries"),
            maximum_lifetime_risk=_decimal(payload, "maximum_lifetime_risk"),
            equity_floor=_decimal(payload, "equity_floor"),
            maximum_quantity=_integer(payload, "maximum_quantity"),
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID") from error


def opportunity_snapshot_request_from_payload(value: object) -> OpportunitySnapshotRequest:
    payload = _mapping(value)
    if set(payload) != {
        "account_role",
        "account_fingerprint",
        "underlying",
        "benchmark",
        "decision_boundary",
        "minimum_expiry",
        "maximum_expiry",
        "minimum_strike",
        "maximum_strike",
        "maximum_contracts",
        "maximum_quote_age_seconds",
        "maximum_quote_skew_seconds",
    }:
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    try:
        return OpportunitySnapshotRequest(
            account_role=AccountRole(_string(payload, "account_role")),
            expected_account_fingerprint=_string(payload, "account_fingerprint"),
            underlying=_string(payload, "underlying"),
            benchmark=_string(payload, "benchmark"),
            decision_boundary=_datetime(payload, "decision_boundary"),
            minimum_expiry=date.fromisoformat(_string(payload, "minimum_expiry")),
            maximum_expiry=date.fromisoformat(_string(payload, "maximum_expiry")),
            minimum_strike=_decimal(payload, "minimum_strike"),
            maximum_strike=_decimal(payload, "maximum_strike"),
            maximum_contracts=_integer(payload, "maximum_contracts"),
            maximum_quote_age=timedelta(seconds=_number(payload, "maximum_quote_age_seconds")),
            maximum_quote_skew=timedelta(seconds=_number(payload, "maximum_quote_skew_seconds")),
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID") from error


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    return value


def _string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    return item


def _integer(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    return item


def _number(value: dict[str, object], key: str) -> int | float:
    item = value.get(key)
    if not isinstance(item, int | float) or isinstance(item, bool) or not math.isfinite(item):
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    return item


def _decimal(value: dict[str, object], key: str) -> Decimal:
    item = value.get(key)
    if not isinstance(item, str):
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    parsed = Decimal(item)
    if not parsed.is_finite():
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    return parsed


def _datetime(value: dict[str, object], key: str) -> datetime:
    item = _string(value, key)
    parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    return parsed.astimezone(UTC)


def _duration(value: dict[str, object], key: str) -> timedelta:
    item = _mapping(value.get(key))
    if set(item) != {"days", "seconds", "microseconds"}:
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID")
    return timedelta(
        days=_integer(item, "days"),
        seconds=_integer(item, "seconds"),
        microseconds=_integer(item, "microseconds"),
    )


def _hash(domain: str, value: object) -> str:
    try:
        payload = json.dumps(
            {"domain": domain, "value": _json_value(value)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as error:
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID") from error
    return hashlib.sha256(payload).hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("NONFINITE_DECIMAL")
        fixed = format(value, "f")
        if "." in fixed:
            fixed = fixed.rstrip("0").rstrip(".")
        return "0" if fixed in {"-0", "+0"} else fixed
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("NAIVE_DATETIME")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return {
            "days": value.days,
            "seconds": value.seconds,
            "microseconds": value.microseconds,
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("NONFINITE_FLOAT")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("NON_STRING_JSON_KEY")
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _verify_plan_row(
    row: DevelopmentOpportunityPlanRow,
    material: dict[str, object],
    plan_hash: str,
    plan_id: UUID,
) -> None:
    stored_material: dict[str, object] = {
        "account_role": row.account_role,
        "opportunity_key": row.opportunity_key,
        "version": row.version,
        "underlying": row.underlying,
        "benchmark_symbol": row.benchmark_symbol,
        "event_session": row.event_session.isoformat(),
        "pre_event_session": row.pre_event_session.isoformat(),
        "reaction_session": row.reaction_session.isoformat(),
        "signal_session": row.signal_session.isoformat(),
        "daily_start_session": row.daily_start_session.isoformat(),
        "allowed_event_codes": row.allowed_event_codes,
        "evidence_window_start": _stored_utc(row.evidence_window_start).isoformat(),
        "evidence_window_end": _stored_utc(row.evidence_window_end).isoformat(),
        "policy_payload": row.policy_payload,
        "policy_hash": row.policy_hash,
        "request_contract": row.request_contract,
        "request_contract_hash": row.request_contract_hash,
        "thesis_code": row.thesis_code,
        "thesis_target_contract": row.thesis_target_contract,
        "thesis_target_hash": row.thesis_target_hash,
        "exposure_limit_contract": row.exposure_limit_contract,
        "exposure_limit_hash": row.exposure_limit_hash,
        "invalidation_codes": row.invalidation_codes,
        "frozen_at": _stored_utc(row.frozen_at).isoformat(),
    }
    if (
        row.plan_id != plan_id
        or row.plan_hash != plan_hash
        or row.plan_material != material
        or stored_material != material
        or _hash("alphadecay.opportunity.plan.v1", stored_material) != row.plan_hash
        or _policy_payload_hash(row.policy_payload) != row.policy_hash
        or _plain_hash(row.request_contract) != row.request_contract_hash
        or _hash("alphadecay.opportunity.thesis-target.v1", row.thesis_target_contract)
        != row.thesis_target_hash
        or _hash("alphadecay.opportunity.exposure-limit.v1", row.exposure_limit_contract)
        != row.exposure_limit_hash
        or row.policy_hash != material["policy_hash"]
        or row.request_contract_hash != material["request_contract_hash"]
        or row.thesis_target_hash != material["thesis_target_hash"]
        or row.exposure_limit_hash != material["exposure_limit_hash"]
    ):
        raise OpportunityEvidenceError("OPPORTUNITY_PLAN_VERSION_CONFLICT")


def _policy_payload_hash(payload: object) -> str:
    return opportunity_policy_hash(opportunity_policy_from_payload(payload))


def _verify_baseline_row(
    row: DevelopmentOpportunityBaselineRow,
    material: dict[str, object],
    baseline_hash: str,
    baseline_id: UUID,
) -> None:
    stored_material: dict[str, object] = {
        "plan_id": str(row.plan_id),
        "account_role": row.account_role,
        "account_fingerprint": row.account_fingerprint,
        "account_source_hash": row.account_source_hash,
        "positions_manifest": row.positions_manifest,
        "positions_source_hash": row.positions_source_hash,
        "positions_complete": row.positions_complete,
        "orders_manifest": row.orders_manifest,
        "orders_source_hash": row.orders_source_hash,
        "orders_complete": row.orders_complete,
        "activity_manifest": row.activity_manifest,
        "activity_source_hash": row.activity_source_hash,
        "activity_complete": row.activity_complete,
        "book_hash": row.book_hash,
        "history_hash": row.history_hash,
        "captured_at": _stored_utc(row.captured_at).isoformat(),
    }
    if row.account_role == AccountRole.SUBMISSION.value:
        stored_material["submission_baseline_id"] = str(row.submission_baseline_id)
    if (
        row.baseline_id != baseline_id
        or row.baseline_hash != baseline_hash
        or row.baseline_material != material
        or stored_material != material
        or _hash("alphadecay.opportunity.baseline.v1", stored_material) != row.baseline_hash
    ):
        raise OpportunityEvidenceError("OPPORTUNITY_BASELINE_CONFLICT")


def _verify_observation_row(
    row: OpportunityObservationManifestRow,
    material: dict[str, object],
    manifest_hash: str,
    observation_id: UUID,
) -> None:
    stored_material: dict[str, object] = {
        "plan_id": str(row.plan_id),
        "baseline_id": str(row.baseline_id),
        "account_role": row.account_role,
        "account_fingerprint": row.account_fingerprint,
        "policy_hash": row.policy_hash,
        "request_hash": row.request_hash,
        "snapshot_hash": row.snapshot_hash,
        "calendar_hash": row.calendar_hash,
        "daily_hash": row.daily_hash,
        "intraday_hash": row.intraday_hash,
        "signal_authority_hash": row.signal_authority_hash,
        "halt_hash": row.halt_hash,
        "catalyst_hash": row.catalyst_hash,
        "greek_hash": row.greek_hash,
        "account_hash": row.account_hash,
        "activity_hash": row.activity_hash,
        "budget_hash": row.budget_hash,
        "prior_decision_hash": row.prior_decision_hash,
        "trusted_at": _stored_utc(row.trusted_at).isoformat(),
        "evaluated_at": _stored_utc(row.evaluated_at).isoformat(),
    }
    if (
        row.observation_id != observation_id
        or row.manifest_hash != manifest_hash
        or row.observation_material != material
        or stored_material != material
        or _hash("alphadecay.opportunity.observation.v1", stored_material) != row.manifest_hash
    ):
        raise OpportunityEvidenceError("OPPORTUNITY_OBSERVATION_CONFLICT")


def _request_material(request: OpportunitySnapshotRequest) -> dict[str, object]:
    return {
        "account_role": request.account_role.value,
        "account_fingerprint": request.expected_account_fingerprint,
        "underlying": request.underlying,
        "benchmark": request.benchmark,
        "decision_boundary": request.decision_boundary.isoformat(),
        "minimum_expiry": request.minimum_expiry.isoformat(),
        "maximum_expiry": request.maximum_expiry.isoformat(),
        "minimum_strike": str(request.minimum_strike),
        "maximum_strike": str(request.maximum_strike),
        "maximum_contracts": request.maximum_contracts,
        "maximum_quote_age_seconds": request.maximum_quote_age.total_seconds(),
        "maximum_quote_skew_seconds": request.maximum_quote_skew.total_seconds(),
    }


def _plain_hash(value: object) -> str:
    try:
        payload = json.dumps(
            _json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as error:
        raise OpportunityEvidenceError("OPPORTUNITY_EVIDENCE_PAYLOAD_INVALID") from error
    return hashlib.sha256(payload).hexdigest()


def _persisted_plan(row: DevelopmentOpportunityPlanRow) -> PersistedOpportunityPlan:
    return PersistedOpportunityPlan(
        plan_id=row.plan_id,
        opportunity_key=row.opportunity_key,
        version=row.version,
        policy_hash=row.policy_hash,
        request_contract_hash=row.request_contract_hash,
        plan_hash=row.plan_hash,
        frozen_at=_stored_utc(row.frozen_at),
        account_role=AccountRole(row.account_role),
    )


def _persisted_baseline(
    row: DevelopmentOpportunityBaselineRow,
) -> PersistedOpportunityBaseline:
    return PersistedOpportunityBaseline(
        baseline_id=row.baseline_id,
        plan_id=row.plan_id,
        account_fingerprint=row.account_fingerprint,
        baseline_hash=row.baseline_hash,
        captured_at=_stored_utc(row.captured_at),
        account_role=AccountRole(row.account_role),
        submission_baseline_id=row.submission_baseline_id,
    )


def _persisted_observation(
    row: OpportunityObservationManifestRow,
) -> PersistedOpportunityObservation:
    return PersistedOpportunityObservation(
        observation_id=row.observation_id,
        plan_id=row.plan_id,
        baseline_id=row.baseline_id,
        account_fingerprint=row.account_fingerprint,
        manifest_hash=row.manifest_hash,
        trusted_at=_stored_utc(row.trusted_at),
        evaluated_at=_stored_utc(row.evaluated_at),
        account_role=AccountRole(row.account_role),
    )
