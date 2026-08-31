from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import Enum
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.alpaca.opportunity import (
    OpportunityMarketSnapshot,
    OpportunityOption,
    OpportunitySnapshotRequest,
    opportunity_market_snapshot_digest,
    opportunity_snapshot_request_digest,
)
from backend.app.contracts.v1 import (
    AccountRole,
    GreekExposure,
    PositionIntent,
    ThesisCreateRequest,
    ThesisResponse,
)
from backend.app.domain.exposure import GreekLeg, aggregate_greeks
from backend.app.execution import FrozenThesisVersion
from backend.app.persistence.opportunity_evidence import (
    OpportunityBaselineSeal,
    OpportunityObservationSpec,
    OpportunityPlanSpec,
    PersistedOpportunityBaseline,
    PersistedOpportunityObservation,
    PersistedOpportunityPlan,
    opportunity_baseline_digest,
    opportunity_observation_digest,
    opportunity_plan_digest,
)
from backend.app.persistence.sqlalchemy_models import AccountRoleRow, ThesisVersionRow
from backend.app.policy import (
    OpportunityDecisionRecord,
    OpportunityOutcome,
    VerticalCandidate,
    VolatilityView,
)
from backend.app.policy.opportunity import evaluate_opportunity, opportunity_policy_hash
from backend.app.services.opportunity_input import (
    AccountBudgetAuthority,
    CatalystAuthority,
    OpportunityInputAssembly,
    OpportunityInputAuthorityError,
    OpportunitySignalAuthority,
    PriorDecisionAuthority,
    assemble_opportunity_input,
)
from backend.app.services.opportunity_selection import (
    CandidateSelectionAuthority,
    CandidateSelectionResult,
    SelectionReason,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")
_OPPORTUNITY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_UNDERLYING = re.compile(r"^[A-Z]{1,6}$")
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_TARGET_KEYS = {"target_at", "volatility_view"}
_LIMIT_KEYS = {
    "delta_low",
    "delta_high",
    "vega_low",
    "vega_high",
    "maximum_daily_theta",
    "minimum_dte",
    "maximum_dte",
    "portfolio_risk_cap",
}
_ORIGIN_KEYS = {
    "plan_id",
    "plan_hash",
    "baseline_id",
    "baseline_hash",
    "observation_id",
    "observation_hash",
    "account_fingerprint",
    "opportunity_key",
    "underlying",
    "policy_hash",
    "input_authority_hash",
    "signal_authority_hash",
    "decision_hash",
    "decision_boundary",
    "candidate_hash",
    "selected_option_sources",
    "atm_call_source_hash",
    "atm_put_source_hash",
    "thesis_code",
    "target_at",
    "intended_exposure",
    "exposure_limits",
    "volatility_view",
    "entry_atm_iv",
    "approved_max_loss",
    "portfolio_risk_cap",
    "invalidation_codes",
    "frozen_at",
}


class OpportunityThesisError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OpportunityThesisFactoryInput:
    plan_spec: OpportunityPlanSpec
    plan: PersistedOpportunityPlan
    baseline_seal: OpportunityBaselineSeal
    baseline: PersistedOpportunityBaseline
    observation_spec: OpportunityObservationSpec
    observation: PersistedOpportunityObservation
    request: OpportunitySnapshotRequest
    snapshot: OpportunityMarketSnapshot
    requested_maximum_quantity: int
    selection_authority: CandidateSelectionAuthority
    selection: CandidateSelectionResult
    signals: OpportunitySignalAuthority
    catalyst: CatalystAuthority
    account: AccountBudgetAuthority
    prior_decision: PriorDecisionAuthority
    assembly: OpportunityInputAssembly
    decision: OpportunityDecisionRecord
    signal_calendar_hash: str
    signal_daily_hash: str
    signal_intraday_hash: str
    signal_authority_hash: str
    budget_source_hash: str


def build_frozen_opportunity_thesis(
    inputs: OpportunityThesisFactoryInput,
) -> FrozenThesisVersion:
    _validate_lineage(inputs)
    try:
        expected_assembly = assemble_opportunity_input(
            request=inputs.request,
            snapshot=inputs.snapshot,
            policy=inputs.plan_spec.policy,
            requested_maximum_quantity=inputs.requested_maximum_quantity,
            selection_authority=inputs.selection_authority,
            selection=inputs.selection,
            signals=inputs.signals,
            catalyst=inputs.catalyst,
            account=inputs.account,
            prior_decision=inputs.prior_decision,
        )
    except OpportunityInputAuthorityError as error:
        raise OpportunityThesisError("THESIS_INPUT_AUTHORITY_MISMATCH") from error
    if expected_assembly != inputs.assembly:
        raise OpportunityThesisError("THESIS_INPUT_AUTHORITY_MISMATCH")
    expected_decision = evaluate_opportunity(inputs.plan_spec.policy, inputs.assembly.values)
    if expected_decision != inputs.decision:
        raise OpportunityThesisError("THESIS_DECISION_AUTHORITY_MISMATCH")
    if (
        inputs.decision.outcome is not OpportunityOutcome.ENTRY_APPROVED
        or inputs.decision.quantity is None
        or inputs.decision.approved_max_loss is None
        or inputs.assembly.values.candidate is None
        or inputs.selection.reason is not SelectionReason.SELECTED
    ):
        raise OpportunityThesisError("THESIS_ENTRY_NOT_APPROVED")

    candidate = inputs.assembly.values.candidate
    intended = _intended_exposure(inputs.snapshot, candidate)
    target_at, volatility_view = _target_contract(inputs.plan_spec)
    limits, portfolio_risk_cap = _exposure_contract(inputs.plan_spec)
    _validate_exposure_contract(
        limits,
        portfolio_risk_cap,
        intended,
        candidate.dte,
        inputs.decision.approved_max_loss,
    )
    selected_options = _selected_options(inputs.snapshot, candidate)
    entry_atm_iv, atm_call_hash, atm_put_hash = _paired_atm_iv(inputs.snapshot, candidate)
    frozen_at = inputs.assembly.values.evaluated_at
    _validate_lifecycle_schema(
        version=inputs.plan.version,
        frozen_at=frozen_at,
        target_at=target_at,
        entry_atm_iv=entry_atm_iv,
        approved_max_loss=inputs.decision.approved_max_loss,
        portfolio_risk_cap=portfolio_risk_cap,
        invalidation_codes=inputs.plan_spec.invalidation_codes,
    )
    thesis_id = _stable_uuid(
        "alphadecay.opportunity.thesis.v1",
        str(inputs.plan.plan_id),
        inputs.account.account_fingerprint,
        inputs.decision.opportunity_key,
    )
    intended_payload = intended.model_dump(mode="json")
    thesis_material = {
        "plan_id": str(inputs.plan.plan_id),
        "plan_hash": inputs.plan.plan_hash,
        "baseline_id": str(inputs.baseline.baseline_id),
        "baseline_hash": inputs.baseline.baseline_hash,
        "observation_id": str(inputs.observation.observation_id),
        "observation_hash": inputs.observation.manifest_hash,
        "account_fingerprint": inputs.account.account_fingerprint,
        "opportunity_key": inputs.decision.opportunity_key,
        "underlying": inputs.plan_spec.underlying,
        "policy_hash": inputs.decision.policy_hash,
        "input_authority_hash": inputs.assembly.authority_hash,
        "signal_authority_hash": inputs.signal_authority_hash,
        "decision_hash": inputs.decision.result_hash,
        "decision_boundary": inputs.decision.decision_boundary,
        "candidate_hash": inputs.decision.candidate_hash,
        "selected_option_sources": [item.source_hash for item in selected_options],
        "atm_call_source_hash": atm_call_hash,
        "atm_put_source_hash": atm_put_hash,
        "thesis_code": inputs.plan_spec.thesis_code,
        "target_at": target_at,
        "intended_exposure": intended_payload,
        "exposure_limits": limits,
        "volatility_view": volatility_view,
        "entry_atm_iv": entry_atm_iv,
        "approved_max_loss": inputs.decision.approved_max_loss,
        "portfolio_risk_cap": portfolio_risk_cap,
        "invalidation_codes": inputs.plan_spec.invalidation_codes,
        "frozen_at": frozen_at,
    }
    origin_hash = _canonical_hash("alphadecay.opportunity.thesis-origin.v1", thesis_material)
    thesis_payload = ThesisResponse(
        thesis_id=thesis_id,
        version=inputs.plan.version,
        frozen=True,
        thesis_hash="0" * 64,
        thesis=ThesisCreateRequest(
            underlying=inputs.plan_spec.underlying,
            thesis_code=inputs.plan_spec.thesis_code,
            invalidation_codes=inputs.plan_spec.invalidation_codes,
            intended_exposure=intended,
            source_policy_hash=inputs.decision.policy_hash,
        ),
    ).model_dump(mode="json")
    thesis_payload["origin_hash"] = origin_hash
    thesis_payload["origin_material"] = _canonical_value(thesis_material)
    draft = FrozenThesisVersion(
        thesis_version_id=UUID(int=0),
        thesis_id=thesis_id,
        account_role=inputs.plan_spec.account_role,
        version=inputs.plan.version,
        thesis_hash="0" * 64,
        policy_hash=inputs.decision.policy_hash,
        underlying=inputs.plan_spec.underlying,
        thesis_code=inputs.plan_spec.thesis_code,
        frozen_at=frozen_at,
        target_at=target_at,
        intended_exposure=intended_payload,
        exposure_limits=limits,
        volatility_view=volatility_view.value,
        entry_atm_iv=entry_atm_iv,
        approved_max_loss=inputs.decision.approved_max_loss,
        portfolio_risk_cap=portfolio_risk_cap,
        invalidation_codes=inputs.plan_spec.invalidation_codes,
        thesis_payload=thesis_payload,
        created_at=frozen_at,
        origin_hash=origin_hash,
    )
    return finalize_frozen_opportunity_thesis(draft, inputs.plan.version)


def finalize_frozen_opportunity_thesis(
    draft: FrozenThesisVersion, account_version: int
) -> FrozenThesisVersion:
    _validate_persistence_material(draft)
    if (
        not isinstance(account_version, int)
        or isinstance(account_version, bool)
        or account_version <= 0
    ):
        raise OpportunityThesisError("THESIS_PERSISTENCE_INPUT_INVALID")
    version_id = _stable_uuid(
        "alphadecay.opportunity.thesis-version.v2",
        str(draft.thesis_id),
        str(account_version),
        draft.origin_hash,
    )
    payload = dict(draft.thesis_payload)
    payload.update(
        thesis_id=str(draft.thesis_id),
        version=account_version,
        thesis_hash="0" * 64,
        origin_hash=draft.origin_hash,
    )
    candidate = replace(
        draft,
        thesis_version_id=version_id,
        version=account_version,
        thesis_hash="0" * 64,
        thesis_payload=payload,
    )
    thesis_hash = _canonical_hash(
        "alphadecay.lifecycle.thesis.v2", _database_thesis_material(candidate)
    )
    payload["thesis_hash"] = thesis_hash
    return replace(candidate, thesis_hash=thesis_hash, thesis_payload=payload)


class SQLAlchemyOpportunityThesisRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def persist(self, draft: FrozenThesisVersion) -> FrozenThesisVersion:
        try:
            canonical_draft = finalize_frozen_opportunity_thesis(draft, draft.version)
        except OpportunityThesisError:
            raise
        except (TypeError, ValueError) as error:
            raise OpportunityThesisError("THESIS_PERSISTENCE_INPUT_INVALID") from error
        if canonical_draft != draft:
            raise OpportunityThesisError("THESIS_PERSISTENCE_INPUT_INVALID")
        try:
            with self._sessions.begin() as session:
                account = session.scalar(
                    select(AccountRoleRow)
                    .where(AccountRoleRow.role == draft.account_role.value)
                    .with_for_update()
                )
                if account is None:
                    raise OpportunityThesisError(f"{draft.account_role.value}_ACCOUNT_MISSING")
                origin = draft.thesis_payload["origin_material"]
                if origin["account_fingerprint"] != account.account_fingerprint:
                    raise OpportunityThesisError("THESIS_ACCOUNT_AUTHORITY_MISMATCH")
                existing = session.scalar(
                    select(ThesisVersionRow).where(
                        ThesisVersionRow.origin_hash == draft.origin_hash
                    )
                )
                if existing is not None:
                    return _persisted_thesis(existing, draft)
                latest = session.scalar(
                    select(func.max(ThesisVersionRow.version)).where(
                        ThesisVersionRow.account_role == draft.account_role.value
                    )
                )
                thesis = finalize_frozen_opportunity_thesis(draft, (latest or 0) + 1)
                row = _thesis_row(thesis)
                session.add(row)
                session.flush()
                session.refresh(row)
                return _persisted_thesis(row, draft)
        except IntegrityError as error:
            with self._sessions() as session:
                existing = session.scalar(
                    select(ThesisVersionRow).where(
                        ThesisVersionRow.origin_hash == draft.origin_hash
                    )
                )
                if existing is not None:
                    return _persisted_thesis(existing, draft)
            raise OpportunityThesisError("THESIS_PERSISTENCE_CONFLICT") from error


def _thesis_row(thesis: FrozenThesisVersion) -> ThesisVersionRow:
    return ThesisVersionRow(
        thesis_version_id=thesis.thesis_version_id,
        thesis_id=thesis.thesis_id,
        account_role=thesis.account_role.value,
        version=thesis.version,
        origin_hash=thesis.origin_hash,
        thesis_hash=thesis.thesis_hash,
        policy_hash=thesis.policy_hash,
        underlying=thesis.underlying,
        thesis_code=thesis.thesis_code,
        frozen_at=thesis.frozen_at,
        target_at=thesis.target_at,
        intended_exposure=thesis.intended_exposure,
        exposure_limits=thesis.exposure_limits,
        volatility_view=thesis.volatility_view,
        entry_atm_iv=thesis.entry_atm_iv,
        approved_max_loss=thesis.approved_max_loss,
        portfolio_risk_cap=thesis.portfolio_risk_cap,
        invalidation_codes=list(thesis.invalidation_codes),
        thesis_payload=thesis.thesis_payload,
        created_at=thesis.created_at,
    )


def _persisted_thesis(
    row: ThesisVersionRow, requested_draft: FrozenThesisVersion
) -> FrozenThesisVersion:
    thesis = FrozenThesisVersion(
        thesis_version_id=row.thesis_version_id,
        thesis_id=row.thesis_id,
        account_role=AccountRole(row.account_role),
        version=row.version,
        thesis_hash=row.thesis_hash,
        policy_hash=row.policy_hash,
        underlying=row.underlying,
        thesis_code=row.thesis_code,
        frozen_at=_stored_time(row.frozen_at),
        target_at=_stored_time(row.target_at),
        intended_exposure=dict(row.intended_exposure),
        exposure_limits=dict(row.exposure_limits),
        volatility_view=row.volatility_view,
        entry_atm_iv=row.entry_atm_iv,
        approved_max_loss=row.approved_max_loss,
        portfolio_risk_cap=row.portfolio_risk_cap,
        invalidation_codes=tuple(row.invalidation_codes),
        thesis_payload=dict(row.thesis_payload),
        created_at=_stored_time(row.created_at),
        origin_hash=row.origin_hash,
    )
    expected = finalize_frozen_opportunity_thesis(requested_draft, thesis.version)
    if thesis != expected:
        raise OpportunityThesisError("THESIS_PERSISTED_REPLAY_MISMATCH")
    return thesis


def _validate_persistence_material(draft: FrozenThesisVersion) -> None:
    origin = draft.thesis_payload.get("origin_material")
    if (
        draft.account_role not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}
        or draft.origin_hash is None
        or not _HASH.fullmatch(draft.origin_hash)
        or not isinstance(origin, dict)
        or set(origin) != _ORIGIN_KEYS
        or draft.origin_hash != _canonical_hash("alphadecay.opportunity.thesis-origin.v1", origin)
        or draft.created_at != draft.frozen_at
        or not _is_strict_utc(draft.created_at)
        or not _is_strict_utc(draft.target_at)
    ):
        raise OpportunityThesisError("THESIS_PERSISTENCE_INPUT_INVALID")

    try:
        plan_id = UUID(str(origin["plan_id"]))
        baseline_id = UUID(str(origin["baseline_id"]))
        observation_id = UUID(str(origin["observation_id"]))
        account_fingerprint = str(origin["account_fingerprint"])
        opportunity_key = str(origin["opportunity_key"])
        expected_thesis_id = _stable_uuid(
            "alphadecay.opportunity.thesis.v1",
            str(plan_id),
            account_fingerprint,
            opportunity_key,
        )
        expected_payload = ThesisResponse(
            thesis_id=draft.thesis_id,
            version=draft.version,
            frozen=True,
            thesis_hash=draft.thesis_hash,
            thesis=ThesisCreateRequest(
                underlying=draft.underlying,
                thesis_code=draft.thesis_code,
                invalidation_codes=draft.invalidation_codes,
                intended_exposure=GreekExposure.model_validate(draft.intended_exposure),
                source_policy_hash=draft.policy_hash,
            ),
        ).model_dump(mode="json")
        expected_payload["origin_hash"] = draft.origin_hash
        expected_payload["origin_material"] = origin
        _validate_lifecycle_schema(
            version=draft.version,
            frozen_at=draft.frozen_at,
            target_at=draft.target_at,
            entry_atm_iv=draft.entry_atm_iv,
            approved_max_loss=draft.approved_max_loss,
            portfolio_risk_cap=draft.portfolio_risk_cap,
            invalidation_codes=draft.invalidation_codes,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OpportunityThesisError("THESIS_PERSISTENCE_INPUT_INVALID") from error

    hash_fields = (
        "plan_hash",
        "baseline_hash",
        "observation_hash",
        "account_fingerprint",
        "policy_hash",
        "input_authority_hash",
        "signal_authority_hash",
        "decision_hash",
        "candidate_hash",
        "atm_call_source_hash",
        "atm_put_source_hash",
    )
    selected_sources = origin["selected_option_sources"]
    if (
        draft.thesis_id != expected_thesis_id
        or draft.thesis_payload != expected_payload
        or not isinstance(origin["plan_id"], str)
        or origin["plan_id"] != str(plan_id)
        or not isinstance(origin["baseline_id"], str)
        or origin["baseline_id"] != str(baseline_id)
        or not isinstance(origin["observation_id"], str)
        or origin["observation_id"] != str(observation_id)
        or any(
            not isinstance(origin[key], str) or not _HASH.fullmatch(origin[key])
            for key in hash_fields
        )
        or not isinstance(selected_sources, list)
        or len(selected_sources) != 2
        or any(not isinstance(item, str) or not _HASH.fullmatch(item) for item in selected_sources)
        or not _OPPORTUNITY_KEY.fullmatch(opportunity_key)
        or origin["opportunity_key"] != opportunity_key
        or not isinstance(draft.underlying, str)
        or not _UNDERLYING.fullmatch(draft.underlying)
        or not isinstance(draft.thesis_code, str)
        or not _REASON_CODE.fullmatch(draft.thesis_code)
        or draft.volatility_view not in {item.value for item in VolatilityView}
        or not isinstance(draft.exposure_limits, dict)
        or len(set(draft.invalidation_codes)) != len(draft.invalidation_codes)
        or any(
            not isinstance(item, str) or not _REASON_CODE.fullmatch(item)
            for item in draft.invalidation_codes
        )
        or origin["underlying"] != draft.underlying
        or origin["policy_hash"] != draft.policy_hash
        or origin["thesis_code"] != draft.thesis_code
        or origin["target_at"] != _canonical_value(draft.target_at)
        or origin["intended_exposure"] != draft.intended_exposure
        or origin["exposure_limits"] != draft.exposure_limits
        or origin["volatility_view"] != draft.volatility_view
        or origin["entry_atm_iv"] != _canonical_decimal(draft.entry_atm_iv)
        or origin["approved_max_loss"] != _canonical_decimal(draft.approved_max_loss)
        or origin["portfolio_risk_cap"] != _canonical_decimal(draft.portfolio_risk_cap)
        or origin["invalidation_codes"] != list(draft.invalidation_codes)
        or origin["frozen_at"] != _canonical_value(draft.frozen_at)
        or not _is_canonical_utc(origin["decision_boundary"])
    ):
        raise OpportunityThesisError("THESIS_PERSISTENCE_INPUT_INVALID")


def _database_thesis_material(thesis: FrozenThesisVersion) -> dict[str, object]:
    payload = dict(thesis.thesis_payload)
    payload.pop("thesis_hash", None)
    return {
        "thesis_version_id": str(thesis.thesis_version_id),
        "thesis_id": str(thesis.thesis_id),
        "account_role": thesis.account_role.value,
        "version": thesis.version,
        "origin_hash": thesis.origin_hash,
        "policy_hash": thesis.policy_hash,
        "underlying": thesis.underlying,
        "thesis_code": thesis.thesis_code,
        "frozen_at": _canonical_value(thesis.frozen_at),
        "target_at": _canonical_value(thesis.target_at),
        "intended_exposure": thesis.intended_exposure,
        "exposure_limits": thesis.exposure_limits,
        "volatility_view": thesis.volatility_view,
        "entry_atm_iv": _canonical_decimal(thesis.entry_atm_iv.quantize(Decimal("0.00000001"))),
        "approved_max_loss": _canonical_decimal(
            thesis.approved_max_loss.quantize(Decimal("0.000001"))
        ),
        "portfolio_risk_cap": _canonical_decimal(
            thesis.portfolio_risk_cap.quantize(Decimal("0.000001"))
        ),
        "invalidation_codes": list(thesis.invalidation_codes),
        "thesis_payload": payload,
    }


def _stored_time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _is_strict_utc(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
        and value == value.astimezone(UTC)
    )


def _is_canonical_utc(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return _canonical_value(parsed) == value


def _validate_lineage(inputs: OpportunityThesisFactoryInput) -> None:
    role = inputs.plan_spec.account_role
    if (
        role not in {AccountRole.DEVELOPMENT, AccountRole.SUBMISSION}
        or inputs.plan.account_role is not role
        or inputs.baseline_seal.account_role is not role
        or inputs.baseline.account_role is not role
        or inputs.observation_spec.account_role is not role
        or inputs.observation.account_role is not role
        or inputs.request.account_role is not role
        or inputs.account.account_role is not role
        or inputs.snapshot.account_book.account.role is not role
    ):
        raise OpportunityThesisError("THESIS_ROLE_AUTHORITY_MISMATCH")
    plan_hash = opportunity_plan_digest(inputs.plan_spec)
    baseline_hash = opportunity_baseline_digest(inputs.baseline_seal)
    observation_hash = opportunity_observation_digest(inputs.observation_spec)
    expected_plan_id = uuid5(NAMESPACE_URL, f"alphadecay:opportunity-plan:{plan_hash}")
    expected_baseline_id = uuid5(NAMESPACE_URL, f"alphadecay:opportunity-baseline:{baseline_hash}")
    expected_observation_id = uuid5(
        NAMESPACE_URL, f"alphadecay:opportunity-observation:{observation_hash}"
    )
    if (
        inputs.plan.plan_id != expected_plan_id
        or inputs.plan.plan_hash != plan_hash
        or inputs.plan.opportunity_key != inputs.plan_spec.opportunity_key
        or inputs.plan.version != inputs.plan_spec.version
        or inputs.plan.policy_hash != opportunity_policy_hash(inputs.plan_spec.policy)
        or inputs.plan.request_contract_hash
        != opportunity_snapshot_request_digest(inputs.plan_spec.request_contract)
        or inputs.plan.frozen_at != inputs.plan_spec.frozen_at
    ):
        raise OpportunityThesisError("THESIS_PLAN_AUTHORITY_MISMATCH")
    if (
        inputs.baseline.baseline_id != expected_baseline_id
        or inputs.baseline.baseline_hash != baseline_hash
        or inputs.baseline.plan_id != inputs.plan.plan_id
        or inputs.baseline_seal.plan_id != inputs.plan.plan_id
        or inputs.baseline.account_fingerprint != inputs.baseline_seal.account_fingerprint
        or inputs.baseline.captured_at != inputs.baseline_seal.captured_at
        or inputs.baseline.submission_baseline_id != inputs.baseline_seal.submission_baseline_id
    ):
        raise OpportunityThesisError("THESIS_BASELINE_AUTHORITY_MISMATCH")
    if (
        inputs.observation.observation_id != expected_observation_id
        or inputs.observation.manifest_hash != observation_hash
        or inputs.observation.plan_id != inputs.plan.plan_id
        or inputs.observation.baseline_id != inputs.baseline.baseline_id
        or inputs.observation_spec.plan_id != inputs.plan.plan_id
        or inputs.observation_spec.baseline_id != inputs.baseline.baseline_id
        or inputs.observation.trusted_at != inputs.observation_spec.trusted_at
        or inputs.observation.evaluated_at != inputs.observation_spec.evaluated_at
        or inputs.observation.account_fingerprint != inputs.observation_spec.account_fingerprint
    ):
        raise OpportunityThesisError("THESIS_OBSERVATION_AUTHORITY_MISMATCH")
    source_hashes = (
        inputs.signal_calendar_hash,
        inputs.signal_daily_hash,
        inputs.signal_intraday_hash,
        inputs.signal_authority_hash,
        inputs.budget_source_hash,
    )
    if any(not isinstance(item, str) or not _HASH.fullmatch(item) for item in source_hashes):
        raise OpportunityThesisError("THESIS_SOURCE_HASH_INVALID")
    observation = inputs.observation_spec
    if (
        inputs.request != inputs.plan_spec.request_contract
        or inputs.request.expected_account_fingerprint != inputs.account.account_fingerprint
        or inputs.request.expected_account_fingerprint
        != inputs.snapshot.account_book.account_fingerprint
        or inputs.account.account_fingerprint != inputs.baseline_seal.account_fingerprint
        or observation.account_fingerprint != inputs.account.account_fingerprint
        or observation.account_fingerprint != inputs.baseline.account_fingerprint
        or observation.policy_hash != inputs.plan.policy_hash
        or observation.request_hash != opportunity_snapshot_request_digest(inputs.request)
        or observation.request_hash != inputs.snapshot.request_hash
        or observation.snapshot_hash != inputs.snapshot.source_hash
        or inputs.snapshot.source_hash != opportunity_market_snapshot_digest(inputs.snapshot)
        or observation.calendar_hash != inputs.signal_calendar_hash
        or observation.daily_hash != inputs.signal_daily_hash
        or observation.intraday_hash != inputs.signal_intraday_hash
        or observation.signal_authority_hash != inputs.signal_authority_hash
        or inputs.signal_authority_hash != inputs.signals.calculation_source_hash
        or observation.halt_hash != inputs.signals.trading_status_source_hash
        or observation.catalyst_hash != inputs.catalyst.source_hash
        or observation.greek_hash != inputs.selection_authority.greek_unit_evidence_hash
        or observation.account_hash != inputs.account.snapshot_book_source_hash
        or observation.activity_hash != inputs.account.history_source_hash
        or observation.budget_hash != inputs.budget_source_hash
        or observation.prior_decision_hash != inputs.prior_decision.source_hash
        or inputs.account.baseline_source_hash != inputs.baseline.baseline_hash
        or inputs.plan.frozen_at > inputs.baseline.captured_at
        or inputs.baseline.captured_at > inputs.observation.trusted_at
        or inputs.observation.trusted_at > inputs.observation.evaluated_at
        or inputs.observation.trusted_at != inputs.snapshot.trusted_at
    ):
        raise OpportunityThesisError("THESIS_SOURCE_AUTHORITY_MISMATCH")


def _intended_exposure(
    snapshot: OpportunityMarketSnapshot, candidate: VerticalCandidate
) -> GreekExposure:
    selected = _selected_options(snapshot, candidate)
    legs = []
    for leg, option in zip(candidate.legs, selected, strict=True):
        if (
            option.underlying != leg.underlying
            or option.expiry != leg.expiry
            or option.strike != leg.strike
            or option.right != leg.right.value[0]
            or leg.multiplier != 100
            or leg.ratio <= 0
            or not leg.greeks_complete
            or not leg.greek_units_verified
        ):
            raise OpportunityThesisError("THESIS_SELECTED_OPTION_MISMATCH")
        legs.append(
            GreekLeg(
                contracts=candidate.quantity * leg.ratio,
                is_long=leg.intent is PositionIntent.BUY_TO_OPEN,
                delta=option.delta,
                gamma=option.gamma,
                theta_per_day=option.theta_per_day,
                vega_per_iv_point=option.vega_per_iv_point,
            )
        )
    try:
        return aggregate_greeks(tuple(legs))
    except ValueError as error:
        raise OpportunityThesisError("THESIS_GREEK_INPUT_INVALID") from error


def _selected_options(
    snapshot: OpportunityMarketSnapshot, candidate: VerticalCandidate
) -> tuple[OpportunityOption, ...]:
    by_symbol: dict[str, list[OpportunityOption]] = {}
    for option in snapshot.options:
        by_symbol.setdefault(option.symbol, []).append(option)
    selected = []
    for leg in candidate.legs:
        matches = by_symbol.get(leg.symbol, [])
        if len(matches) != 1:
            raise OpportunityThesisError("THESIS_SELECTED_OPTION_NOT_UNIQUE")
        selected.append(matches[0])
    return tuple(selected)


def _paired_atm_iv(
    snapshot: OpportunityMarketSnapshot, candidate: VerticalCandidate
) -> tuple[Decimal, str, str]:
    expiry = candidate.legs[0].expiry
    pairs: dict[Decimal, dict[str, OpportunityOption]] = {}
    for option in snapshot.options:
        if option.expiry == expiry and option.implied_volatility.is_finite():
            pair = pairs.setdefault(option.strike, {})
            if option.right in pair:
                raise OpportunityThesisError("THESIS_ATM_IV_PAIR_AMBIGUOUS")
            pair[option.right] = option
    complete = [
        (abs(strike - snapshot.underlying_bar.close), strike, pair["C"], pair["P"])
        for strike, pair in pairs.items()
        if set(pair) == {"C", "P"}
        and pair["C"].implied_volatility > 0
        and pair["P"].implied_volatility > 0
    ]
    if not complete:
        raise OpportunityThesisError("THESIS_ATM_IV_PAIR_MISSING")
    _, _, call, put = min(complete, key=lambda item: (item[0], item[1]))
    return (
        (call.implied_volatility + put.implied_volatility) / 2,
        call.source_hash,
        put.source_hash,
    )


def _target_contract(plan: OpportunityPlanSpec) -> tuple[datetime, VolatilityView]:
    contract = plan.thesis_target_contract
    if set(contract) != _TARGET_KEYS:
        raise OpportunityThesisError("THESIS_TARGET_CONTRACT_INVALID")
    try:
        target_at = datetime.fromisoformat(str(contract["target_at"]).replace("Z", "+00:00"))
        volatility_view = VolatilityView(contract["volatility_view"])
    except (TypeError, ValueError) as error:
        raise OpportunityThesisError("THESIS_TARGET_CONTRACT_INVALID") from error
    if (
        target_at.tzinfo is None
        or target_at.utcoffset() != timedelta(0)
        or target_at <= plan.policy.selected_decision_boundary
    ):
        raise OpportunityThesisError("THESIS_TARGET_CONTRACT_INVALID")
    return target_at.astimezone(UTC), volatility_view


def _exposure_contract(plan: OpportunityPlanSpec) -> tuple[dict[str, object], Decimal]:
    contract = plan.exposure_limit_contract
    if set(contract) != _LIMIT_KEYS:
        raise OpportunityThesisError("THESIS_EXPOSURE_CONTRACT_INVALID")
    try:
        limits: dict[str, object] = {
            key: _canonical_decimal(_decimal(contract[key]))
            for key in (
                "delta_low",
                "delta_high",
                "vega_low",
                "vega_high",
                "maximum_daily_theta",
            )
        }
        limits["minimum_dte"] = _integer(contract["minimum_dte"])
        limits["maximum_dte"] = _integer(contract["maximum_dte"])
        maximum_relative_spread = plan.policy.maximum_relative_spread.quantize(
            Decimal("0.0000000001"),
            rounding=ROUND_FLOOR,
        )
        limits["maximum_relative_spread"] = _canonical_decimal(maximum_relative_spread)
        limits["liquidity_authority_hash"] = _canonical_hash(
            "alphadecay.lifecycle-liquidity-authority.v1",
            {
                "policy_hash": opportunity_policy_hash(plan.policy),
                "maximum_relative_spread": maximum_relative_spread,
            },
        )
        portfolio_risk_cap = _decimal(contract["portfolio_risk_cap"])
    except (TypeError, ValueError) as error:
        raise OpportunityThesisError("THESIS_EXPOSURE_CONTRACT_INVALID") from error
    return limits, portfolio_risk_cap


def _validate_exposure_contract(
    limits: dict[str, object],
    portfolio_risk_cap: Decimal,
    intended: GreekExposure,
    candidate_dte: int,
    approved_max_loss: Decimal,
) -> None:
    delta_low = _decimal(limits["delta_low"])
    delta_high = _decimal(limits["delta_high"])
    vega_low = _decimal(limits["vega_low"])
    vega_high = _decimal(limits["vega_high"])
    maximum_daily_theta = _decimal(limits["maximum_daily_theta"])
    minimum_dte = limits["minimum_dte"]
    maximum_dte = limits["maximum_dte"]
    maximum_relative_spread = _decimal(limits["maximum_relative_spread"])
    liquidity_authority_hash = limits["liquidity_authority_hash"]
    if (
        not delta_low < delta_high
        or not vega_low < vega_high
        or maximum_daily_theta <= 0
        or not 1 <= minimum_dte <= maximum_dte
        or not 0 < maximum_relative_spread < 1
        or not isinstance(liquidity_authority_hash, str)
        or not _HASH.fullmatch(liquidity_authority_hash)
        or not minimum_dte <= candidate_dte <= maximum_dte
        or portfolio_risk_cap <= 0
        or approved_max_loss > portfolio_risk_cap
        or not delta_low <= intended.delta <= delta_high
        or not vega_low <= intended.vega_per_iv_point <= vega_high
        or abs(intended.theta_per_day) > maximum_daily_theta
    ):
        raise OpportunityThesisError("THESIS_EXPOSURE_CONTRACT_INVALID")


def _validate_lifecycle_schema(
    *,
    version: int,
    frozen_at: datetime,
    target_at: datetime,
    entry_atm_iv: Decimal,
    approved_max_loss: Decimal,
    portfolio_risk_cap: Decimal,
    invalidation_codes: tuple[str, ...],
) -> None:
    maximum_money = Decimal("100000")
    if (
        type(version) is not int
        or version <= 0
        or target_at <= frozen_at
        or not Decimal(0) < entry_atm_iv <= Decimal(100)
        or entry_atm_iv != entry_atm_iv.quantize(Decimal("0.00000001"))
        or not Decimal(0) < approved_max_loss <= maximum_money
        or approved_max_loss != approved_max_loss.quantize(Decimal("0.000001"))
        or not Decimal(0) < portfolio_risk_cap <= maximum_money
        or portfolio_risk_cap != portfolio_risk_cap.quantize(Decimal("0.000001"))
        or approved_max_loss > portfolio_risk_cap
        or not 1 <= len(invalidation_codes) <= 32
    ):
        raise OpportunityThesisError("THESIS_LIFECYCLE_SCHEMA_INVALID")


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError
    return result


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    return value


def _canonical_decimal(value: Decimal) -> str:
    fixed = format(value, "f")
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    return "0" if fixed in {"-0", "+0"} else fixed


def _stable_uuid(domain: str, *values: str) -> UUID:
    encoded = json.dumps((domain, *values), separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return uuid5(NAMESPACE_URL, f"{domain}:{digest}")


def _canonical_hash(domain: str, value: object) -> str:
    payload = json.dumps(
        {"domain": domain, "value": _canonical_value(value)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        fixed = format(value, "f")
        if "." in fixed:
            fixed = fixed.rstrip("0").rstrip(".")
        return "0" if fixed in {"-0", "+0"} else fixed
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_canonical_value(item) for item in value]
    return value
