from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from .models import OrderAttempt
from .reconciliation import (
    ReconciliationExpectation,
    ReconciliationPurpose,
    WholeAccountReconciliation,
)


class AttemptObservationSource(StrEnum):
    DISPATCH_OUTCOME = "DISPATCH_OUTCOME"
    TARGETED_LOOKUP = "TARGETED_LOOKUP"
    TARGETED_LOOKUP_FAILURE = "TARGETED_LOOKUP_FAILURE"


@dataclass(frozen=True)
class AttemptObservation:
    observation_id: UUID
    permit_id: UUID
    intent_id: UUID
    attempt_ordinal: int
    sequence: int
    source: AttemptObservationSource
    observed_attempt: OrderAttempt | None
    observed_at: datetime
    observation_hash: str


@dataclass(frozen=True)
class BrokerMutationPermit:
    permit_id: UUID
    reconciliation_id: UUID
    intent_id: UUID
    intent_digest: str
    claim_token: UUID
    claim_generation: int
    execution_epoch: int
    mutation_kind: ReconciliationPurpose
    attempt_ordinal: int
    generation: int
    predecessor_permit_id: UUID | None
    request_hash: str
    target_client_order_id: str | None
    target_provider_order_id: str | None
    issued_at: datetime
    expires_at: datetime
    state: str
    dispatch_nonce: UUID | None = None
    dispatch_acquired_at: datetime | None = None
    consumed_at: datetime | None = None
    outcome_hash: str | None = None
    limit_price: Decimal | None = None
    quote_hash: str | None = None
    quote_source_timestamps: tuple[datetime, ...] = ()
    quote_retrieved_at: datetime | None = None
    timing_authority_at: datetime | None = None
    prior_request_hash: str | None = None


@dataclass(frozen=True)
class BrokerMutationPreparation:
    reconciliation: WholeAccountReconciliation
    permit: BrokerMutationPermit | None
    attempt: OrderAttempt | None


@dataclass(frozen=True)
class BrokerMutationPlan:
    expectation: ReconciliationExpectation
    attempt: OrderAttempt


@dataclass(frozen=True)
class BrokerMutationSchedule:
    purpose: ReconciliationPurpose
    timing_authority_at: datetime
