from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from uuid import UUID

from backend.app.contracts.v1 import AccountRole
from backend.app.persistence.opportunity_evidence import (
    OpportunityBaselineAuthority,
    OpportunityBaselineSeal,
    OpportunityEvidenceError,
    OpportunityPlanAuthority,
    OpportunityPlanSpec,
    PersistedOpportunityBaseline,
    PersistedOpportunityPlan,
    _json_value,
    _request_material,
    opportunity_baseline_identity,
    opportunity_plan_identity,
    opportunity_policy_from_payload,
    opportunity_snapshot_request_from_payload,
)


class OpportunityBootstrapError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OpportunityBootstrapRepository(Protocol):
    def freeze_plan(self, spec: OpportunityPlanSpec) -> PersistedOpportunityPlan: ...

    def seal_baseline(self, seal: OpportunityBaselineSeal) -> PersistedOpportunityBaseline: ...

    def load_plan(
        self, opportunity_key: str, *, version: int | None = None
    ) -> OpportunityPlanAuthority | None: ...

    def load_baseline(self, plan_id: UUID) -> OpportunityBaselineAuthority | None: ...


@dataclass(frozen=True)
class OpportunityBootstrapInput:
    plan: OpportunityPlanSpec
    baseline: OpportunityBaselineSeal


@dataclass(frozen=True)
class OpportunityBootstrapResult:
    mode: str
    plan_id: UUID
    plan_hash: str
    baseline_id: UUID
    baseline_hash: str

    def sanitized_payload(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "plan_id": str(self.plan_id),
            "plan_hash": self.plan_hash,
            "baseline_id": str(self.baseline_id),
            "baseline_hash": self.baseline_hash,
        }


_PLAN_KEYS = {
    "opportunity_key",
    "version",
    "underlying",
    "event_session",
    "pre_event_session",
    "reaction_session",
    "signal_session",
    "daily_start_session",
    "allowed_event_codes",
    "evidence_window_start",
    "evidence_window_end",
    "policy",
    "request_contract",
    "thesis_code",
    "thesis_target_contract",
    "exposure_limit_contract",
    "invalidation_codes",
    "frozen_at",
}
_BASELINE_KEYS = {
    "plan_id",
    "account_fingerprint",
    "account_source_hash",
    "positions_manifest",
    "positions_source_hash",
    "positions_complete",
    "orders_manifest",
    "orders_source_hash",
    "orders_complete",
    "activity_manifest",
    "activity_source_hash",
    "activity_complete",
    "book_hash",
    "history_hash",
    "captured_at",
}
_POLICY_KEYS = {
    "version",
    "opportunity_key",
    "underlying",
    "selected_decision_boundary",
    "last_entry_boundary",
    "maximum_decision_delay",
    "maximum_underlying_age",
    "maximum_catalyst_age",
    "maximum_option_quote_age",
    "maximum_leg_quote_skew",
    "minimum_vwap_distance",
    "maximum_vwap_distance",
    "minimum_relative_return",
    "minimum_beta",
    "maximum_beta",
    "required_trend_hits",
    "maximum_first_reaction",
    "minimum_catalyst_score",
    "minimum_candidate_score",
    "minimum_dte",
    "maximum_dte",
    "maximum_relative_spread",
    "minimum_debit_width_fraction",
    "maximum_debit_width_fraction",
    "minimum_credit_width_fraction",
    "maximum_position_loss",
    "maximum_equity_risk_fraction",
    "maximum_lifetime_entries",
    "maximum_lifetime_risk",
    "equity_floor",
    "maximum_quantity",
}
_REQUEST_KEYS = {
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
}


def parse_development_opportunity_bootstrap(
    value: object,
) -> OpportunityBootstrapInput:
    payload = _mapping(value)
    if set(payload) != {"account_role", "plan", "baseline"}:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    if payload["account_role"] != AccountRole.DEVELOPMENT.value:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_DEVELOPMENT_ONLY")
    try:
        plan = _parse_plan(_mapping(payload["plan"]))
        baseline = _parse_baseline(_mapping(payload["baseline"]))
    except (OpportunityEvidenceError, TypeError, ValueError, OverflowError) as error:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID") from error
    _identities(plan, baseline)
    return OpportunityBootstrapInput(plan=plan, baseline=baseline)


def parse_development_opportunity_plan(value: object) -> OpportunityPlanSpec:
    payload = _mapping(value)
    try:
        return _parse_plan(payload)
    except (OpportunityEvidenceError, TypeError, ValueError, OverflowError) as error:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID") from error


def development_opportunity_bootstrap_payload(
    bootstrap: OpportunityBootstrapInput,
) -> dict[str, object]:
    if type(bootstrap) is not OpportunityBootstrapInput:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_INPUT_INVALID")
    _identities(bootstrap.plan, bootstrap.baseline)
    plan = bootstrap.plan
    baseline = bootstrap.baseline
    policy = _json_value(plan.policy)
    if not isinstance(policy, dict):
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    payload: dict[str, object] = {
        "account_role": AccountRole.DEVELOPMENT.value,
        "plan": {
            "opportunity_key": plan.opportunity_key,
            "version": plan.version,
            "underlying": plan.underlying,
            "event_session": plan.event_session.isoformat(),
            "pre_event_session": plan.pre_event_session.isoformat(),
            "reaction_session": plan.reaction_session.isoformat(),
            "signal_session": plan.signal_session.isoformat(),
            "daily_start_session": plan.daily_start_session.isoformat(),
            "allowed_event_codes": list(plan.allowed_event_codes),
            "evidence_window_start": plan.evidence_window_start.isoformat(),
            "evidence_window_end": plan.evidence_window_end.isoformat(),
            "policy": policy,
            "request_contract": _request_material(plan.request_contract),
            "thesis_code": plan.thesis_code,
            "thesis_target_contract": plan.thesis_target_contract,
            "exposure_limit_contract": plan.exposure_limit_contract,
            "invalidation_codes": list(plan.invalidation_codes),
            "frozen_at": plan.frozen_at.isoformat(),
        },
        "baseline": {
            "plan_id": str(baseline.plan_id),
            "account_fingerprint": baseline.account_fingerprint,
            "account_source_hash": baseline.account_source_hash,
            "positions_manifest": list(baseline.positions_manifest),
            "positions_source_hash": baseline.positions_source_hash,
            "positions_complete": baseline.positions_complete,
            "orders_manifest": list(baseline.orders_manifest),
            "orders_source_hash": baseline.orders_source_hash,
            "orders_complete": baseline.orders_complete,
            "activity_manifest": list(baseline.activity_manifest),
            "activity_source_hash": baseline.activity_source_hash,
            "activity_complete": baseline.activity_complete,
            "book_hash": baseline.book_hash,
            "history_hash": baseline.history_hash,
            "captured_at": baseline.captured_at.isoformat(),
        },
    }
    parse_development_opportunity_bootstrap(payload)
    return payload


def bootstrap_development_opportunity(
    bootstrap: OpportunityBootstrapInput,
    *,
    persist: bool = False,
    repository: OpportunityBootstrapRepository | None = None,
) -> OpportunityBootstrapResult:
    if type(bootstrap) is not OpportunityBootstrapInput:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_INPUT_INVALID")
    if type(persist) is not bool:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_MODE_INVALID")
    plan_id, plan_hash, baseline_id, baseline_hash = _identities(bootstrap.plan, bootstrap.baseline)
    if not persist:
        return OpportunityBootstrapResult(
            mode="PREVIEW",
            plan_id=plan_id,
            plan_hash=plan_hash,
            baseline_id=baseline_id,
            baseline_hash=baseline_hash,
        )
    if repository is None:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_REPOSITORY_REQUIRED")

    first_plan = repository.freeze_plan(bootstrap.plan)
    first_baseline = repository.seal_baseline(bootstrap.baseline)
    replayed_plan = repository.freeze_plan(bootstrap.plan)
    replayed_baseline = repository.seal_baseline(bootstrap.baseline)
    loaded_plan = repository.load_plan(
        bootstrap.plan.opportunity_key,
        version=bootstrap.plan.version,
    )
    loaded_baseline = repository.load_baseline(plan_id)

    expected_plan = (plan_id, plan_hash)
    expected_baseline = (baseline_id, baseline_hash)
    if (
        first_plan != replayed_plan
        or first_baseline != replayed_baseline
        or (first_plan.plan_id, first_plan.plan_hash) != expected_plan
        or (first_baseline.baseline_id, first_baseline.baseline_hash) != expected_baseline
        or loaded_plan is None
        or loaded_baseline is None
        or loaded_plan.spec != bootstrap.plan
        or loaded_plan.persisted != first_plan
        or loaded_baseline.seal != bootstrap.baseline
        or loaded_baseline.persisted != first_baseline
    ):
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_REPLAY_MISMATCH")
    return OpportunityBootstrapResult(
        mode="PERSISTED",
        plan_id=plan_id,
        plan_hash=plan_hash,
        baseline_id=baseline_id,
        baseline_hash=baseline_hash,
    )


def _identities(
    plan: OpportunityPlanSpec,
    baseline: OpportunityBaselineSeal,
) -> tuple[UUID, str, UUID, str]:
    if type(plan) is not OpportunityPlanSpec or type(baseline) is not OpportunityBaselineSeal:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_INPUT_INVALID")
    try:
        plan_id, plan_hash = opportunity_plan_identity(plan)
        baseline_id, baseline_hash = opportunity_baseline_identity(baseline)
    except OpportunityEvidenceError as error:
        raise OpportunityBootstrapError(error.code) from error
    if (
        plan.account_role is not AccountRole.DEVELOPMENT
        or plan.request_contract.account_role is not AccountRole.DEVELOPMENT
        or baseline.account_role is not AccountRole.DEVELOPMENT
        or baseline.plan_id != plan_id
        or baseline.account_fingerprint != plan.request_contract.expected_account_fingerprint
        or baseline.captured_at < plan.frozen_at
    ):
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_AUTHORITY_MISMATCH")
    return plan_id, plan_hash, baseline_id, baseline_hash


def _parse_plan(payload: dict[str, object]) -> OpportunityPlanSpec:
    if set(payload) != _PLAN_KEYS:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    policy = _mapping(payload["policy"])
    request = _mapping(payload["request_contract"])
    if set(policy) != _POLICY_KEYS or set(request) != _REQUEST_KEYS:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    request_contract = opportunity_snapshot_request_from_payload(request)
    return OpportunityPlanSpec(
        opportunity_key=_string(payload, "opportunity_key"),
        version=_integer(payload, "version"),
        underlying=_string(payload, "underlying"),
        event_session=_date(payload, "event_session"),
        pre_event_session=_date(payload, "pre_event_session"),
        reaction_session=_date(payload, "reaction_session"),
        signal_session=_date(payload, "signal_session"),
        daily_start_session=_date(payload, "daily_start_session"),
        allowed_event_codes=_strings(payload, "allowed_event_codes"),
        evidence_window_start=_datetime(payload, "evidence_window_start"),
        evidence_window_end=_datetime(payload, "evidence_window_end"),
        policy=opportunity_policy_from_payload(policy),
        request_contract=request_contract,
        thesis_code=_string(payload, "thesis_code"),
        thesis_target_contract=_document(payload, "thesis_target_contract"),
        exposure_limit_contract=_document(payload, "exposure_limit_contract"),
        invalidation_codes=_strings(payload, "invalidation_codes"),
        frozen_at=_datetime(payload, "frozen_at"),
        account_role=request_contract.account_role,
    )


def _parse_baseline(payload: dict[str, object]) -> OpportunityBaselineSeal:
    if set(payload) != _BASELINE_KEYS:
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    return OpportunityBaselineSeal(
        plan_id=UUID(_string(payload, "plan_id")),
        account_fingerprint=_string(payload, "account_fingerprint"),
        account_source_hash=_string(payload, "account_source_hash"),
        positions_manifest=_manifest(payload, "positions_manifest"),
        positions_source_hash=_string(payload, "positions_source_hash"),
        positions_complete=_boolean(payload, "positions_complete"),
        orders_manifest=_manifest(payload, "orders_manifest"),
        orders_source_hash=_string(payload, "orders_source_hash"),
        orders_complete=_boolean(payload, "orders_complete"),
        activity_manifest=_manifest(payload, "activity_manifest"),
        activity_source_hash=_string(payload, "activity_source_hash"),
        activity_complete=_boolean(payload, "activity_complete"),
        book_hash=_string(payload, "book_hash"),
        history_hash=_string(payload, "history_hash"),
        captured_at=_datetime(payload, "captured_at"),
    )


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    return value


def _string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    return value


def _integer(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    return value


def _boolean(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    return value


def _date(payload: dict[str, object], key: str) -> date:
    return date.fromisoformat(_string(payload, key))


def _datetime(payload: dict[str, object], key: str) -> datetime:
    value = datetime.fromisoformat(_string(payload, key).replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    return value.astimezone(UTC)


def _strings(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    return tuple(value)


def _document(payload: dict[str, object], key: str) -> dict[str, object]:
    return dict(_mapping(payload.get(key)))


def _manifest(payload: dict[str, object], key: str) -> tuple[dict[str, object], ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PAYLOAD_INVALID")
    return tuple(dict(item) for item in value)
