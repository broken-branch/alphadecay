from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.app.contracts.v1 import AccountRole
from backend.app.execution import (
    EntryApprovalAuthorization,
    ExecutionAction,
    OrderEnvelope,
    OrderLegIntent,
    intent_digest,
    order_envelope_hash,
)
from backend.app.execution.models import ExecutionIntent, IntentState
from backend.app.lifecycle import LifecycleLaunchAuthority
from backend.app.policy import (
    OpportunityDecisionRecord,
    OpportunityInput,
    OpportunityOutcome,
    OpportunityPolicy,
    evaluate_opportunity,
)

from .acquisition import AuthorizationIntentProposal, OpportunityAcquisition

_HASH = re.compile(r"^[0-9a-f]{64}$")
_AUTHORIZATION_TTL = timedelta(seconds=30)


@dataclass(frozen=True)
class EntryProposalAuthorityInput:
    policy: OpportunityPolicy
    values: OpportunityInput
    decision: OpportunityDecisionRecord
    thesis_version_id: UUID
    thesis_account_role: AccountRole
    thesis_policy_hash: str
    thesis_underlying: str
    thesis_frozen_at: datetime
    account_role: AccountRole
    account_fingerprint: str
    valid_from: datetime
    expires_at: datetime
    benchmark_symbol: str
    underlying_source_hash: str
    benchmark_source_hash: str
    completed_bar_source_hash: str


@dataclass(frozen=True)
class BuiltEntryProposalAuthority:
    authorization: EntryApprovalAuthorization
    envelope: OrderEnvelope
    intent: ExecutionIntent
    proposal: AuthorizationIntentProposal
    launch_authority: LifecycleLaunchAuthority
    acquisition: OpportunityAcquisition


def build_development_entry_proposal(
    inputs: EntryProposalAuthorityInput,
) -> BuiltEntryProposalAuthority:
    _validate_inputs(inputs)
    decision = evaluate_opportunity(inputs.policy, inputs.values)
    if decision != inputs.decision or decision.outcome is not OpportunityOutcome.ENTRY_APPROVED:
        raise ValueError("ENTRY_DECISION_AUTHORITY_INVALID")

    candidate = inputs.values.candidate
    if candidate is None or decision.quantity is None or decision.approved_max_loss is None:
        raise ValueError("ENTRY_CANDIDATE_AUTHORITY_INVALID")

    approval_id = _stable_uuid(
        "alphadecay.entry-approval.v1",
        decision.result_hash,
        str(inputs.thesis_version_id),
        inputs.account_fingerprint,
        inputs.thesis_frozen_at.isoformat(),
        inputs.valid_from.isoformat(),
        inputs.expires_at.isoformat(),
        inputs.underlying_source_hash,
        inputs.benchmark_source_hash,
        inputs.completed_bar_source_hash,
    )
    envelope = OrderEnvelope(
        action=ExecutionAction.ENTRY,
        authorization_certificate_id=approval_id,
        policy_hash=decision.policy_hash,
        account_fingerprint=inputs.account_fingerprint,
        position_or_book_fingerprint=decision.book_fingerprint,
        legs=tuple(OrderLegIntent(leg.symbol, leg.intent, leg.ratio) for leg in candidate.legs),
        quantity=decision.quantity,
        minimum_limit=candidate.approved_limit,
        maximum_limit=candidate.approved_limit,
        approved_max_loss=decision.approved_max_loss,
        event_key=decision.opportunity_key,
        trading_day=decision.decision_boundary.date(),
    )
    envelope_hash = order_envelope_hash(envelope)
    authorization = EntryApprovalAuthorization(
        approval_id=approval_id,
        thesis_version_id=inputs.thesis_version_id,
        account_role=inputs.account_role,
        policy_hash=decision.policy_hash,
        book_fingerprint=decision.book_fingerprint,
        envelope_hash=envelope_hash,
        approved_max_loss=decision.approved_max_loss,
        quantity=decision.quantity,
        valid_from=inputs.valid_from,
        expires_at=inputs.expires_at,
    )
    digest = intent_digest(envelope)
    intent = ExecutionIntent(
        intent_id=_stable_uuid("alphadecay.entry-intent.v1", digest),
        account_role=inputs.account_role,
        envelope=envelope,
        digest=digest,
        state=IntentState.APPROVED,
    )
    proposal = AuthorizationIntentProposal(authorization, intent)
    launch_authority = LifecycleLaunchAuthority(
        beta60=inputs.values.beta,
        benchmark_symbol=inputs.benchmark_symbol,
        entry_boundary_at=inputs.values.completed_bar_at,
        entry_policy_hash=decision.policy_hash,
        underlying_source_hash=inputs.underlying_source_hash,
        benchmark_source_hash=inputs.benchmark_source_hash,
        completed_bar_source_hash=inputs.completed_bar_source_hash,
    )
    acquisition = OpportunityAcquisition(
        policy=inputs.policy,
        values=inputs.values,
        thesis_version_id=inputs.thesis_version_id,
        proposal=proposal,
        launch_authority=launch_authority,
    )
    return BuiltEntryProposalAuthority(
        authorization=authorization,
        envelope=envelope,
        intent=intent,
        proposal=proposal,
        launch_authority=launch_authority,
        acquisition=acquisition,
    )


def _validate_inputs(inputs: EntryProposalAuthorityInput) -> None:
    values = inputs.values
    times = (
        values.observed_decision_boundary,
        values.completed_bar_at,
        values.evaluated_at,
        inputs.thesis_frozen_at,
        inputs.valid_from,
        inputs.expires_at,
    )
    hashes = (
        inputs.account_fingerprint,
        inputs.thesis_policy_hash,
        inputs.underlying_source_hash,
        inputs.benchmark_source_hash,
        inputs.completed_bar_source_hash,
    )
    if inputs.account_role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION):
        raise ValueError("ENTRY_ACCOUNT_ROLE_INVALID")
    if any(not _is_utc(value) for value in times):
        raise ValueError("ENTRY_AUTHORITY_TIME_INVALID")
    if not all(_HASH.fullmatch(value) for value in hashes):
        raise ValueError("ENTRY_AUTHORITY_HASH_INVALID")
    if (
        inputs.thesis_account_role is not inputs.account_role
        or values.account.account_role is not inputs.account_role
        or values.account.book_fingerprint != inputs.decision.book_fingerprint
    ):
        raise ValueError("ENTRY_ACCOUNT_BINDING_INVALID")
    if (
        not isinstance(inputs.thesis_version_id, UUID)
        or inputs.thesis_underlying != inputs.policy.underlying
        or values.underlying != inputs.thesis_underlying
        or inputs.thesis_policy_hash != inputs.decision.policy_hash
    ):
        raise ValueError("ENTRY_THESIS_BINDING_INVALID")
    if (
        values.completed_bar_at != values.observed_decision_boundary
        or values.completed_bar_at > inputs.thesis_frozen_at
        or inputs.thesis_frozen_at > inputs.valid_from
        or inputs.valid_from != values.evaluated_at
        or inputs.expires_at <= inputs.valid_from
        or inputs.expires_at - inputs.valid_from > _AUTHORIZATION_TTL
    ):
        raise ValueError("ENTRY_AUTHORITY_CHRONOLOGY_INVALID")
    if inputs.benchmark_symbol != "QQQ" or inputs.values.beta <= 0:
        raise ValueError("ENTRY_LAUNCH_AUTHORITY_INVALID")


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _stable_uuid(domain: str, *values: str) -> UUID:
    encoded = json.dumps((domain, *values), separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return uuid5(NAMESPACE_URL, f"{domain}:{digest}")
