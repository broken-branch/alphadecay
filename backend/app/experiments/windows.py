from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.contracts.v1.models import ContractModel, Money, UtcDateTime
from backend.app.persistence.sqlalchemy_models import (
    AgentDecisionRow,
    AgentInputSnapshotRow,
    AgentTickRow,
    AssessmentCertificateRow,
    DevelopmentOpportunityPlanRow,
    EntryApprovalCertificateRow,
    ExecutionCertificateRow,
    ExecutionIntentRow,
    ManagedLifecyclePositionRow,
    ManagedPositionSnapshotRow,
    ManagedPositionTransitionRow,
)
from backend.app.policy.opportunity import (
    STRUCTURAL_BEARISH_OTM_PILOT_ID,
    STRUCTURAL_BULLISH_OTM_PILOT_ID,
    STRUCTURAL_BULLISH_PILOT_ID,
)

_STRUCTURAL_PILOT_COPY = {
    STRUCTURAL_BULLISH_PILOT_ID: (
        "SPY structural bullish beta pilot",
        "Bullish direction fixed before the window; one bull call debit spread, "
        "30–45 days to expiry, $4 wide, with defined risk.",
    ),
    STRUCTURAL_BULLISH_OTM_PILOT_ID: (
        "SPY structural bullish OTM pilot",
        "Bullish direction fixed before the window; one out-of-the-money bull call debit "
        "spread, 30–45 days to expiry, $4 wide, with defined risk.",
    ),
    STRUCTURAL_BEARISH_OTM_PILOT_ID: (
        "SPY structural bearish OTM pilot",
        "Bearish direction fixed before the window; one out-of-the-money bear put debit "
        "spread, 30–45 days to expiry, $4 wide, with defined risk.",
    ),
}


class ExperimentWindowReadError(RuntimeError):
    pass


class ExperimentWindowProtocol(ContractModel):
    schema_version: Literal["v2"] = "v2"
    name: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=320)


class ExperimentWindowInterval(ContractModel):
    schema_version: Literal["v2"] = "v2"
    opens_at: UtcDateTime
    closes_at: UtcDateTime

    @model_validator(mode="after")
    def ordered(self) -> ExperimentWindowInterval:
        if self.closes_at < self.opens_at:
            raise ValueError("experiment entry window is reversed")
        return self


class ExperimentWindowDecision(ContractModel):
    schema_version: Literal["v2"] = "v2"
    outcome_code: Literal["ENTRY_APPROVED", "NO_TRADE", "PROVIDER_FAILURE_NO_TRADE"]
    reason: str = Field(min_length=1, max_length=240)
    decided_at: UtcDateTime


class ExperimentWindowLifecycle(ContractModel):
    schema_version: Literal["v2"] = "v2"
    status: Literal["OPEN", "CLOSED"]
    opened_at: UtcDateTime
    closed_at: UtcDateTime | None
    exit_reason: str | None
    realized_paper_pnl: Money | None

    @model_validator(mode="after")
    def status_matches_close(self) -> ExperimentWindowLifecycle:
        closed = self.status == "CLOSED"
        if closed != (self.closed_at is not None):
            raise ValueError("experiment lifecycle close status is inconsistent")
        if closed != (self.exit_reason is not None):
            raise ValueError("experiment lifecycle exit reason is inconsistent")
        if not closed and self.realized_paper_pnl is not None:
            raise ValueError("open experiment cannot report realized pnl")
        if self.closed_at is not None and self.closed_at < self.opened_at:
            raise ValueError("experiment lifecycle chronology is inconsistent")
        return self


class ExperimentWindow(ContractModel):
    schema_version: Literal["v2"] = "v2"
    plan_version: int = Field(gt=0)
    protocol: ExperimentWindowProtocol
    frozen_at: UtcDateTime
    decision_boundary: UtcDateTime
    entry_window: ExperimentWindowInterval
    terminal_decision: ExperimentWindowDecision | None
    lifecycle: ExperimentWindowLifecycle | None
    status: Literal["PENDING", "OPEN", "DECIDED", "ABORTED"]
    aborted_reason: str | None
    tick_outcome_code: str | None
    tick_outcome_text: str | None
    collapsed_versions: tuple[int, ...]

    @model_validator(mode="after")
    def chronology(self) -> ExperimentWindow:
        if self.frozen_at > self.decision_boundary:
            raise ValueError("experiment plan was not frozen before its decision")
        if self.decision_boundary != self.entry_window.opens_at:
            raise ValueError("experiment decision boundary must open the entry window")
        if self.lifecycle is not None and (
            self.terminal_decision is None
            or self.terminal_decision.outcome_code != "ENTRY_APPROVED"
        ):
            raise ValueError("experiment lifecycle requires an approved entry")
        if (self.status == "ABORTED") != (self.aborted_reason is not None):
            raise ValueError("experiment abort reason is inconsistent")
        if (self.tick_outcome_code is None) != (self.tick_outcome_text is None):
            raise ValueError("experiment tick outcome is inconsistent")
        return self


class ExperimentWindowListResponse(ContractModel):
    schema_version: Literal["v2"] = "v2"
    windows: tuple[ExperimentWindow, ...]


@dataclass(frozen=True)
class _ResolvedDecision:
    decision: AgentDecisionRow
    tick: AgentTickRow


class SQLAlchemyExperimentWindowReader:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def list(self) -> tuple[ExperimentWindow, ...]:
        try:
            with self._sessions() as session:
                plans = tuple(
                    session.scalars(
                        select(DevelopmentOpportunityPlanRow)
                        .where(DevelopmentOpportunityPlanRow.account_role == "SUBMISSION")
                        .order_by(DevelopmentOpportunityPlanRow.frozen_at.desc())
                    )
                )
                windows = tuple(self._window(session, plan) for plan in plans)
                return _collapse_aborted_windows(windows)
        except ExperimentWindowReadError:
            raise
        except (SQLAlchemyError, TypeError, ValueError) as error:
            raise ExperimentWindowReadError("EXPERIMENT_WINDOWS_UNAVAILABLE") from error

    def _window(
        self,
        session: Session,
        plan: DevelopmentOpportunityPlanRow,
    ) -> ExperimentWindow:
        policy = _mapping(plan.policy_payload)
        boundary = _datetime(policy, "selected_decision_boundary")
        entry_close = _datetime(policy, "last_entry_boundary")
        resolved = self._terminal_decision(session, plan, boundary, entry_close)
        decision = None if resolved is None else resolved.decision
        lifecycle = None if decision is None else self._lifecycle(session, decision)
        status, aborted_reason = self._status(session, plan, boundary, entry_close, decision)
        return ExperimentWindow(
            plan_version=plan.version,
            protocol=_protocol(plan, policy),
            frozen_at=_utc(plan.frozen_at),
            decision_boundary=boundary,
            entry_window=ExperimentWindowInterval(
                opens_at=boundary,
                closes_at=entry_close,
            ),
            terminal_decision=(
                None
                if decision is None
                else ExperimentWindowDecision(
                    outcome_code=decision.outcome,
                    reason=_decision_reason(decision.reason_code),
                    decided_at=_utc(decision.created_at),
                )
            ),
            lifecycle=lifecycle,
            status=status,
            aborted_reason=aborted_reason,
            tick_outcome_code=None if resolved is None else resolved.tick.terminal_code,
            tick_outcome_text=(
                None if resolved is None else _tick_outcome_text(resolved.tick.terminal_code)
            ),
            collapsed_versions=(plan.version,),
        )

    @staticmethod
    def _terminal_decision(
        session: Session,
        plan: DevelopmentOpportunityPlanRow,
        boundary: datetime,
        entry_close: datetime,
    ) -> _ResolvedDecision | None:
        policy_rows = tuple(
            session.scalars(
                select(AgentDecisionRow)
                .join(AgentTickRow, AgentTickRow.tick_id == AgentDecisionRow.origin_tick_id)
                .where(
                    AgentDecisionRow.account_role == "SUBMISSION",
                    AgentDecisionRow.decision_kind == "OPPORTUNITY",
                    AgentDecisionRow.decision_boundary == boundary,
                    AgentDecisionRow.outcome.in_(("ENTRY_APPROVED", "NO_TRADE")),
                    AgentDecisionRow.policy_hash == plan.policy_hash,
                    AgentTickRow.status == "COMPLETED",
                    AgentTickRow.decision_id == AgentDecisionRow.decision_id,
                    AgentTickRow.account_role == AgentDecisionRow.account_role,
                    AgentTickRow.account_fingerprint == AgentDecisionRow.account_fingerprint,
                    AgentTickRow.completed_at >= AgentDecisionRow.created_at,
                )
                .order_by(AgentDecisionRow.created_at.desc(), AgentDecisionRow.decision_id.desc())
            )
        )
        if len(policy_rows) > 1:
            raise ExperimentWindowReadError("EXPERIMENT_WINDOWS_AMBIGUOUS")
        if policy_rows:
            return _ResolvedDecision(policy_rows[0], _decision_tick(session, policy_rows[0]))
        calibration_rows = tuple(
            row
            for row in session.scalars(
                select(AgentDecisionRow)
                .join(AgentTickRow, AgentTickRow.tick_id == AgentDecisionRow.origin_tick_id)
                .where(
                    AgentDecisionRow.account_role == "SUBMISSION",
                    AgentDecisionRow.decision_kind == "OPPORTUNITY",
                    AgentDecisionRow.decision_boundary == boundary,
                    AgentDecisionRow.outcome == "NO_TRADE",
                    AgentDecisionRow.reason_code == "CALIBRATION_BINDING_NO_TRADE",
                    AgentTickRow.status == "COMPLETED",
                    AgentTickRow.decision_id == AgentDecisionRow.decision_id,
                    AgentTickRow.account_role == AgentDecisionRow.account_role,
                    AgentTickRow.account_fingerprint == AgentDecisionRow.account_fingerprint,
                    AgentTickRow.completed_at >= AgentDecisionRow.created_at,
                )
            )
            if _calibration_decision_matches(session, plan, row)
        )
        if len(calibration_rows) > 1:
            raise ExperimentWindowReadError("EXPERIMENT_WINDOWS_AMBIGUOUS")
        if calibration_rows:
            return _ResolvedDecision(
                calibration_rows[0], _decision_tick(session, calibration_rows[0])
            )
        provider_rows = tuple(
            session.scalars(
                select(AgentDecisionRow)
                .join(AgentTickRow, AgentTickRow.tick_id == AgentDecisionRow.origin_tick_id)
                .where(
                    AgentDecisionRow.account_role == "SUBMISSION",
                    AgentDecisionRow.decision_kind == "OPPORTUNITY",
                    AgentDecisionRow.outcome == "PROVIDER_FAILURE_NO_TRADE",
                    AgentDecisionRow.created_at >= boundary,
                    AgentDecisionRow.created_at <= entry_close,
                    AgentTickRow.status == "COMPLETED",
                    AgentTickRow.decision_id == AgentDecisionRow.decision_id,
                    AgentTickRow.account_role == AgentDecisionRow.account_role,
                    AgentTickRow.account_fingerprint == AgentDecisionRow.account_fingerprint,
                    AgentTickRow.completed_at >= AgentDecisionRow.created_at,
                )
                .order_by(AgentDecisionRow.created_at.desc(), AgentDecisionRow.decision_id.desc())
            )
        )
        return (
            _ResolvedDecision(provider_rows[0], _decision_tick(session, provider_rows[0]))
            if provider_rows
            else None
        )

    @staticmethod
    def _status(
        session: Session,
        plan: DevelopmentOpportunityPlanRow,
        boundary: datetime,
        entry_close: datetime,
        decision: AgentDecisionRow | None,
    ) -> tuple[Literal["PENDING", "OPEN", "DECIDED", "ABORTED"], str | None]:
        if decision is not None:
            return "DECIDED", None
        now = datetime.now(UTC)
        if now < boundary:
            return "PENDING", None
        if now <= entry_close:
            return "OPEN", None
        observed = any(
            isinstance(snapshot.normalized_payload, dict)
            and snapshot.normalized_payload.get("opportunity_key") == plan.opportunity_key
            for snapshot in session.scalars(
                select(AgentInputSnapshotRow).where(
                    AgentInputSnapshotRow.account_role == "SUBMISSION",
                    AgentInputSnapshotRow.decision_kind == "OPPORTUNITY",
                    AgentInputSnapshotRow.decision_boundary == boundary,
                    AgentInputSnapshotRow.observed_at >= boundary,
                    AgentInputSnapshotRow.observed_at <= entry_close,
                )
            )
        )
        if not observed:
            return "ABORTED", "runtime never started"
        return "PENDING", None

    @staticmethod
    def _lifecycle(
        session: Session,
        decision: AgentDecisionRow,
    ) -> ExperimentWindowLifecycle | None:
        if decision.outcome != "ENTRY_APPROVED":
            return None
        approval = session.scalar(
            select(EntryApprovalCertificateRow).where(
                EntryApprovalCertificateRow.agent_decision_id == decision.decision_id
            )
        )
        if approval is None:
            return None
        position = session.scalar(
            select(ManagedLifecyclePositionRow).where(
                ManagedLifecyclePositionRow.entry_approval_id == approval.approval_id
            )
        )
        if position is None:
            return None
        if position.closed_at is None:
            return ExperimentWindowLifecycle(
                status="OPEN",
                opened_at=_utc(position.activated_at),
                closed_at=None,
                exit_reason=None,
                realized_paper_pnl=None,
            )
        close = session.scalar(
            select(ManagedPositionTransitionRow)
            .where(
                ManagedPositionTransitionRow.managed_position_id == position.managed_position_id,
                ManagedPositionTransitionRow.action == "CLOSE",
            )
            .order_by(ManagedPositionTransitionRow.transition_sequence.desc())
            .limit(1)
        )
        if close is None or _utc(close.occurred_at) != _utc(position.closed_at):
            raise ExperimentWindowReadError("EXPERIMENT_WINDOWS_LIFECYCLE_INVALID")
        certificate = session.get(ExecutionCertificateRow, close.execution_certificate_id)
        snapshot = session.scalar(
            select(ManagedPositionSnapshotRow).where(
                ManagedPositionSnapshotRow.transition_id == close.transition_id
            )
        )
        certified_pnl = (
            Decimal(snapshot.cumulative_cashflow)
            if certificate is not None
            and certificate.execution_status == "FILLED"
            and certificate.reconciliation_id == close.post_reconciliation_id
            and snapshot is not None
            and snapshot.reconciliation_id == close.post_reconciliation_id
            else None
        )
        return ExperimentWindowLifecycle(
            status="CLOSED",
            opened_at=_utc(position.activated_at),
            closed_at=_utc(position.closed_at),
            exit_reason=_exit_reason(session, close),
            realized_paper_pnl=certified_pnl,
        )


def _protocol(
    plan: DevelopmentOpportunityPlanRow,
    policy: dict[str, object],
) -> ExperimentWindowProtocol:
    minimum_dte = _integer(policy, "minimum_dte")
    maximum_dte = _integer(policy, "maximum_dte")
    structural_copy = _STRUCTURAL_PILOT_COPY.get(plan.opportunity_key)
    if structural_copy is not None:
        return ExperimentWindowProtocol(
            name=structural_copy[0],
            summary=structural_copy[1],
        )
    return ExperimentWindowProtocol(
        name=_plain_protocol_name(plan.thesis_code),
        summary=(
            "Direction follows the frozen confirmation rule; one defined-risk options vertical, "
            f"{minimum_dte}–{maximum_dte} days to expiry."
        ),
    )


def _decision_tick(session: Session, decision: AgentDecisionRow) -> AgentTickRow:
    tick = session.get(AgentTickRow, decision.origin_tick_id)
    if (
        tick is None
        or tick.status != "COMPLETED"
        or tick.decision_id != decision.decision_id
        or tick.terminal_code is None
    ):
        raise ExperimentWindowReadError("EXPERIMENT_WINDOWS_TICK_INVALID")
    return tick


def _collapse_aborted_windows(
    windows: tuple[ExperimentWindow, ...],
) -> tuple[ExperimentWindow, ...]:
    grouped: dict[str, list[ExperimentWindow]] = {}
    for window in windows:
        grouped.setdefault(window.protocol.name, []).append(window)
    collapsed: list[ExperimentWindow] = []
    for protocol_windows in grouped.values():
        ordered = sorted(
            protocol_windows,
            key=lambda item: (item.frozen_at, item.plan_version),
        )
        burst: list[ExperimentWindow] = []
        for window in ordered:
            if (
                burst
                and window.status == "ABORTED"
                and burst[-1].status == "ABORTED"
                and window.frozen_at - burst[-1].frozen_at <= timedelta(minutes=1)
            ):
                burst.append(window)
                continue
            collapsed.extend(_collapse_burst(burst))
            burst = [window]
        collapsed.extend(_collapse_burst(burst))
    return tuple(
        sorted(
            collapsed,
            key=lambda item: (item.decision_boundary, item.frozen_at, item.plan_version),
            reverse=True,
        )
    )


def _collapse_burst(burst: list[ExperimentWindow]) -> tuple[ExperimentWindow, ...]:
    if len(burst) < 2 or burst[0].status != "ABORTED":
        return tuple(burst)
    latest = burst[-1]
    return (
        latest.model_copy(
            update={"collapsed_versions": tuple(item.plan_version for item in burst)}
        ),
    )


def _calibration_decision_matches(
    session: Session,
    plan: DevelopmentOpportunityPlanRow,
    decision: AgentDecisionRow,
) -> bool:
    snapshot = session.get(AgentInputSnapshotRow, decision.input_snapshot_id)
    if snapshot is None or not isinstance(snapshot.normalized_payload, dict):
        return False
    machine_hash = snapshot.normalized_payload.get("machine_binding_hash")
    calibration_hash = snapshot.normalized_payload.get("calibration_hash")
    if not (
        isinstance(machine_hash, str)
        and isinstance(calibration_hash, str)
        and calibration_hash == decision.policy_hash
    ):
        return False
    material = json.dumps(
        {
            "domain": "alphadecay.calibration-machine-binding.v1",
            "account_role": "SUBMISSION",
            "account_fingerprint": decision.account_fingerprint,
            "decision_code": "CALIBRATION_BINDING_NO_TRADE",
            "policy_hash": plan.policy_hash,
            "calibration_hash": decision.policy_hash,
            "decision_boundary": _utc(decision.decision_boundary).isoformat(),
            "sealed_at": _utc(snapshot.observed_at).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(material).hexdigest() == machine_hash


_DECISION_REASONS = {
    "ENTRY_APPROVED": "Every fixed entry and safety rule passed.",
    "PROVIDER_FAILURE_NO_TRADE": ("A required data source failed, so no trade was allowed."),
    "CALIBRATION_BINDING_NO_TRADE": (
        "The frozen calibration required the agent to stop before entry."
    ),
    "DECISION_BOUNDARY_INCOMPLETE": "The required decision bar was not complete.",
    "DECISION_BOUNDARY_MISMATCH": "The observation did not match the frozen decision time.",
    "DECISION_BOUNDARY_NOT_REACHED": "The frozen decision time had not arrived.",
    "DECISION_BOUNDARY_STALE": "The decision evidence arrived too late for this window.",
    "DATA_QUALITY_INCOMPLETE": "Required market or account evidence was incomplete.",
    "NORMALIZED_INPUT_INVALID": "The collected evidence could not be validated.",
    "MARKET_CLOSED": "The market was closed at the decision time.",
    "TRADING_HALT_STATUS_UNKNOWN": "Trading-halt status could not be confirmed.",
    "TRADING_HALTED": "Trading was halted.",
    "UNDERLYING_DATA_STALE": "The underlying market data was too old.",
    "CATALYST_DATA_STALE": "The event evidence was too old.",
    "CATALYST_DATA_MISSING": "Required event evidence was missing.",
    "CATALYST_CONTRADICTED": "Authoritative evidence contradicted the event thesis.",
    "CATALYST_SCORE_BELOW_MINIMUM": "The event evidence did not meet the frozen threshold.",
    "BETA_OUT_OF_BOUNDS": "Measured market sensitivity was outside the frozen range.",
    "FIRST_REACTION_OUT_OF_SUPPORT": "The first market reaction was outside the supported range.",
    "DIRECTION_NOT_CONFIRMED": "The frozen direction rule was not confirmed.",
    "OPTION_CANDIDATE_MISSING": "No eligible defined-risk spread was available.",
    "OPTION_CANDIDATE_INVALID": "The selected spread could not be validated.",
    "OPTION_ONLY_REQUIRED": "The candidate was not an options-only position.",
    "VERTICAL_STRUCTURE_INVALID": "The candidate was not the required vertical spread.",
    "VERTICAL_DIRECTION_MISMATCH": "The spread direction did not match the frozen rule.",
    "OPTION_FEED_NOT_INDICATIVE": "The option quote did not come from the allowed paper-data feed.",
    "OPTION_QUOTE_STALE": "The option quote was too old.",
    "OPTION_QUOTES_UNSYNCHRONIZED": "The two option quotes were too far apart in time.",
    "OPTION_QUOTE_INVALID": "The option quote could not be validated.",
    "OPTION_QUOTE_TOO_WIDE": "The option spread was wider than the frozen limit.",
    "OPTION_GREEKS_MISSING": "Required option risk measures were missing.",
    "OPTION_DTE_OUT_OF_RANGE": "Days to expiry were outside the frozen range.",
    "OPTION_DTE_MISMATCH": "The reported days to expiry did not match the contract date.",
    "OPTION_CONTRACT_INELIGIBLE": "An option contract did not meet the frozen eligibility rules.",
    "OPTION_PAYOFF_INVALID": "The defined-risk payoff could not be verified.",
    "CANDIDATE_SCORE_BELOW_MINIMUM": "No spread met the frozen candidate score.",
    "CANDIDATE_FALLBACK_FORBIDDEN": "Only the highest-ranked eligible spread was allowed.",
    "BUYING_POWER_INSUFFICIENT": "Paper buying power was below the required amount.",
    "QUANTITY_OUT_OF_BOUNDS": "The position size was outside the frozen limit.",
    "ACCOUNT_ROLE_NOT_EXECUTABLE": "The account was not eligible for this paper experiment.",
    "BASELINE_NOT_CLEAN": "The paper account baseline was not clean.",
    "EQUITY_FLOOR_REACHED": "The paper account had reached its equity floor.",
    "ACCOUNT_NOT_FLAT": "Another position was already open.",
    "OPEN_ORDER_EXISTS": "Another paper order was still open.",
    "ENTRY_RESERVATION_ACTIVE": "Another entry was already reserved.",
    "LIFETIME_ENTRY_LIMIT_REACHED": "The experiment had reached its entry limit.",
    "LIFETIME_RISK_LIMIT_REACHED": "The experiment had reached its total risk limit.",
    "EVENT_ALREADY_ATTEMPTED": "This event window had already been attempted.",
    "POSITION_RISK_LIMIT_EXCEEDED": "The candidate exceeded the frozen position-risk limit.",
    "ENTRY_WINDOW_CLOSED": "The frozen entry window had closed.",
    "POLICY_SCOPE_MISMATCH": "The evidence did not match this frozen protocol.",
    "BOOK_FINGERPRINT_INVALID": "The paper account position record could not be verified.",
    "ACCOUNT_STATE_INVALID": "The paper account state could not be validated.",
    "PRIOR_DECISION_BINDING": "An earlier decision for this window was already binding.",
}

_TICK_OUTCOMES = {
    "ENTRY_APPROVED": "Entry approved; execution did not continue.",
    "EXECUTION_BLOCKED": "Entry approved, then execution was blocked before the order was sent.",
    "SUBMISSION_ORDER_PREVIEW_UNAVAILABLE": (
        "Entry approved, but the paper order preview was unavailable."
    ),
    "ENTRY_MATERIALIZATION_PREPARATION_FAILED": (
        "Entry approved, but the paper order could not be prepared."
    ),
    "FILLED": "The paper order filled and the position was reconciled.",
}


def _decision_reason(code: str) -> str:
    return _DECISION_REASONS.get(code, "A recorded safety rule stopped this window.")


def _tick_outcome_text(code: str | None) -> str | None:
    if code is None:
        return None
    return _TICK_OUTCOMES.get(
        code, "The recorded execution outcome is available in the paper record."
    )


def _exit_reason(session: Session, close: ManagedPositionTransitionRow) -> str:
    intent = session.get(ExecutionIntentRow, close.execution_intent_id)
    if intent is None or intent.assessment_certificate_id is None:
        return "The certified lifecycle rules closed the paper position."
    assessment = session.get(AssessmentCertificateRow, intent.assessment_certificate_id)
    if assessment is None or assessment.agent_decision_id is None:
        return "The certified lifecycle rules closed the paper position."
    decision = session.get(AgentDecisionRow, assessment.agent_decision_id)
    if decision is None:
        return "The certified lifecycle rules closed the paper position."
    if "FORCED" in decision.reason_code:
        return "The frozen schedule required the paper position to close."
    if "RISK" in decision.reason_code:
        return "A frozen risk rule required the paper position to close."
    if "THESIS" in decision.reason_code or "CATALYST" in decision.reason_code:
        return "The opening thesis no longer held, so the paper position closed."
    return "The certified lifecycle rules closed the paper position."


def _plain_protocol_name(code: str) -> str:
    words = [word.casefold() for word in code.split("_") if word and not word.isdigit()]
    if not words:
        return "Frozen options protocol"
    return " ".join(words).capitalize()[:120]


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("experiment protocol is invalid")
    return value


def _datetime(value: dict[str, object], key: str) -> datetime:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError("experiment protocol time is invalid")
    return _utc(datetime.fromisoformat(item.replace("Z", "+00:00")))


def _integer(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValueError("experiment protocol number is invalid")
    return item


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
