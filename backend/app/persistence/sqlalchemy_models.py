from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.app.order_limits import (
    MAX_STRUCTURAL_APPROVED_RISK,
    MAX_STRUCTURAL_LIFETIME_ENTRIES,
    MAX_STRUCTURAL_LIFETIME_RISK,
    MAX_STRUCTURAL_OPTION_QUANTITY,
)

JSON_DOCUMENT = JSON().with_variant(JSONB, "postgresql")
JSON_NULLABLE_DOCUMENT = JSON(none_as_null=True).with_variant(
    JSONB(none_as_null=True), "postgresql"
)


class Base(DeclarativeBase):
    pass


class AccountRoleRow(Base):
    __tablename__ = "account_roles"
    __table_args__ = (
        CheckConstraint("role IN ('SUBMISSION', 'DEVELOPMENT')", name="ck_executable_account_role"),
        CheckConstraint(
            "(execution_locked = false AND execution_lock_reason IS NULL "
            "AND execution_locked_at IS NULL AND execution_lock_id IS NULL "
            "AND recovery_pending = false) OR "
            "(execution_locked = true AND execution_lock_reason IN "
            "('ASSIGNMENT_SUSPECTED', 'RECONCILIATION_MISMATCH', "
            "'ENTRY_EQUITY_FLOOR', 'ENTRY_OPEN_POSITION_LIMIT', "
            "'ENTRY_LIMITS_REQUIRED', 'ENTRY_POLICY_AUTHORITY_MISMATCH', "
            "'ENTRY_COUNT_EXHAUSTED', 'ENTRY_POSITION_RISK_EXHAUSTED', "
            "'ENTRY_RISK_EXHAUSTED', 'ENTRY_QUANTITY_EXHAUSTED', "
            "'BROKER_TRANSITION_STALLED', 'UNMANAGED_PARTIAL_EXPOSURE') "
            "AND execution_locked_at IS NOT NULL AND execution_lock_id IS NOT NULL "
            "AND execution_lock_generation > 0)",
            name="ck_account_execution_lock",
        ),
        CheckConstraint(
            "execution_epoch >= 0 AND claim_generation >= 0",
            name="ck_account_execution_fence",
        ),
    )

    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    account_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    equity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    autonomous_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    execution_locked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    execution_lock_reason: Mapped[str | None] = mapped_column(String(40))
    execution_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_lock_id: Mapped[UUID | None] = mapped_column(unique=True)
    execution_lock_generation: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    recovery_pending: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    execution_epoch: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    claim_generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))


class SubmissionBaselineRow(Base):
    __tablename__ = "submission_baselines"

    baseline_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"), unique=True)
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    equity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    positions_hash: Mapped[str] = mapped_column(String(64))
    orders_hash: Mapped[str] = mapped_column(String(64))
    activities_hash: Mapped[str] = mapped_column(String(64))
    contaminated: Mapped[bool] = mapped_column(Boolean, default=False)


class AccountReconciliationStateRow(Base):
    __tablename__ = "account_reconciliation_states"
    __table_args__ = (
        UniqueConstraint("account_role", "sequence"),
        CheckConstraint("sequence > 0", name="ck_reconciliation_state_sequence"),
        CheckConstraint(
            "(sequence = 1 AND authority_permit_id IS NULL "
            "AND authority_observation_id IS NULL "
            "AND authority_permit_request_hash IS NULL) OR "
            "(sequence > 1 AND authority_permit_id IS NOT NULL "
            "AND authority_observation_id IS NOT NULL "
            "AND authority_permit_request_hash IS NOT NULL "
            "AND length(authority_permit_request_hash) = 64)",
            name="ck_reconciliation_state_observation_authority",
        ),
    )

    state_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    sequence: Mapped[int] = mapped_column(BigInteger)
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    baseline_id: Mapped[UUID] = mapped_column(ForeignKey("submission_baselines.baseline_id"))
    baseline_captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expected_cash: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    expected_positions: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    expected_open_orders: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    known_activities: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    activity_complete_through: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_activity_hashes: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list)
    predecessor_state_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("account_reconciliation_states.state_id"), unique=True
    )
    authority_reconciliation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("whole_account_reconciliations.reconciliation_id"), unique=True
    )
    authority_permit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("broker_mutation_permits.permit_id"), unique=True
    )
    authority_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("attempt_observations.observation_id"), unique=True
    )
    authority_permit_request_hash: Mapped[str | None] = mapped_column(String(64))
    transition_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)


class WholeAccountReconciliationRow(Base):
    __tablename__ = "whole_account_reconciliations"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('SUBMIT', 'REPLACE', 'CANCEL', 'BASELINE_INITIALIZATION')",
            name="ck_whole_reconciliation_purpose",
        ),
        CheckConstraint("attempt_ordinal BETWEEN 0 AND 3", name="ck_reconciliation_ordinal"),
    )

    reconciliation_id: Mapped[UUID] = mapped_column(primary_key=True)
    reconciliation_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expectation_hash: Mapped[str] = mapped_column(String(64))
    execution_intent_id: Mapped[UUID] = mapped_column(ForeignKey("execution_intents.intent_id"))
    intent_digest: Mapped[str] = mapped_column(String(64))
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(24))
    attempt_ordinal: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str] = mapped_column(String(64))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expectation_payload: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    sweep_payload: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    positions_manifest_hash: Mapped[str] = mapped_column(String(64))
    orders_manifest_hash: Mapped[str] = mapped_column(String(64))
    activities_manifest_hash: Mapped[str] = mapped_column(String(64))
    safe: Mapped[bool] = mapped_column(Boolean)
    block_codes: Mapped[list[str]] = mapped_column(JSON_DOCUMENT)


class BrokerMutationPermitRow(Base):
    __tablename__ = "broker_mutation_permits"
    __table_args__ = (
        UniqueConstraint(
            "execution_intent_id",
            "mutation_kind",
            "attempt_ordinal",
            "permit_generation",
            name="uq_broker_permit_generation",
        ),
        CheckConstraint(
            "mutation_kind IN ('SUBMIT', 'REPLACE', 'CANCEL')",
            name="ck_broker_permit_mutation",
        ),
        CheckConstraint("attempt_ordinal BETWEEN 0 AND 3", name="ck_broker_permit_ordinal"),
        CheckConstraint("permit_generation > 0", name="ck_broker_permit_generation"),
        CheckConstraint("expires_at > issued_at", name="ck_broker_permit_expiry"),
        CheckConstraint(
            "state IN ('PREPARED', 'DISPATCHING', 'LOOKUP_ONLY', 'CONSUMED', 'EXPIRED')",
            name="ck_broker_permit_state",
        ),
        CheckConstraint(
            "(state = 'PREPARED' AND dispatch_nonce IS NULL "
            "AND dispatch_acquired_at IS NULL AND consumed_at IS NULL "
            "AND outcome_hash IS NULL) OR "
            "(state = 'DISPATCHING' AND dispatch_nonce IS NOT NULL "
            "AND dispatch_acquired_at IS NOT NULL AND consumed_at IS NULL "
            "AND outcome_hash IS NULL) OR "
            "(state = 'LOOKUP_ONLY' AND dispatch_nonce IS NOT NULL "
            "AND dispatch_acquired_at IS NOT NULL AND consumed_at IS NULL "
            "AND outcome_hash IS NULL) OR "
            "(state = 'CONSUMED' AND dispatch_nonce IS NOT NULL "
            "AND dispatch_acquired_at IS NOT NULL AND consumed_at IS NOT NULL "
            "AND outcome_hash IS NOT NULL) OR "
            "(state = 'EXPIRED' AND dispatch_nonce IS NULL "
            "AND dispatch_acquired_at IS NULL AND consumed_at IS NOT NULL "
            "AND outcome_hash IS NULL)",
            name="ck_broker_permit_transition_fields",
        ),
        CheckConstraint(
            "(quote_hash IS NULL AND json_array_length(quote_source_timestamps) = 0 "
            "AND quote_retrieved_at IS NULL AND timing_authority_at IS NULL "
            "AND prior_request_hash IS NULL) OR "
            "(length(quote_hash) = 64 AND quote_hash NOT GLOB '*[^0-9a-f]*' "
            "AND json_array_length(quote_source_timestamps) > 0 "
            "AND quote_retrieved_at IS NOT NULL AND timing_authority_at IS NOT NULL "
            "AND length(prior_request_hash) = 64 "
            "AND prior_request_hash NOT GLOB '*[^0-9a-f]*')",
            name="ck_broker_permit_quote_authority",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "(quote_hash IS NULL AND jsonb_array_length(quote_source_timestamps) = 0 "
            "AND quote_retrieved_at IS NULL AND timing_authority_at IS NULL "
            "AND prior_request_hash IS NULL) OR "
            "(quote_hash ~ '^[0-9a-f]{64}$' "
            "AND jsonb_array_length(quote_source_timestamps) > 0 "
            "AND quote_retrieved_at IS NOT NULL AND timing_authority_at IS NOT NULL "
            "AND prior_request_hash ~ '^[0-9a-f]{64}$')",
            name="ck_broker_permit_quote_authority",
        ).ddl_if(dialect="postgresql"),
        Index(
            "uq_current_dispatchable_broker_permit",
            "execution_intent_id",
            "mutation_kind",
            "attempt_ordinal",
            unique=True,
            postgresql_where=text("state IN ('PREPARED', 'DISPATCHING', 'LOOKUP_ONLY')"),
            sqlite_where=text("state IN ('PREPARED', 'DISPATCHING', 'LOOKUP_ONLY')"),
        ),
    )

    permit_id: Mapped[UUID] = mapped_column(primary_key=True)
    reconciliation_id: Mapped[UUID] = mapped_column(
        ForeignKey("whole_account_reconciliations.reconciliation_id"), unique=True
    )
    execution_intent_id: Mapped[UUID] = mapped_column(ForeignKey("execution_intents.intent_id"))
    intent_digest: Mapped[str] = mapped_column(String(64))
    claim_token: Mapped[UUID] = mapped_column()
    claim_generation: Mapped[int] = mapped_column(BigInteger)
    execution_epoch: Mapped[int] = mapped_column(BigInteger)
    mutation_kind: Mapped[str] = mapped_column(String(16))
    attempt_ordinal: Mapped[int] = mapped_column(Integer)
    permit_generation: Mapped[int] = mapped_column(BigInteger)
    predecessor_permit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("broker_mutation_permits.permit_id"), unique=True
    )
    request_hash: Mapped[str] = mapped_column(String(64))
    limit_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    quote_hash: Mapped[str | None] = mapped_column(String(64))
    quote_source_timestamps: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list)
    quote_retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timing_authority_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prior_request_hash: Mapped[str | None] = mapped_column(String(64))
    target_client_order_id: Mapped[str | None] = mapped_column(String(64))
    target_provider_order_id: Mapped[str | None] = mapped_column(String(128))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16))
    dispatch_nonce: Mapped[UUID | None] = mapped_column(unique=True)
    dispatch_acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome_hash: Mapped[str | None] = mapped_column(String(64))


class AttemptObservationRow(Base):
    __tablename__ = "attempt_observations"
    __table_args__ = (
        UniqueConstraint("execution_intent_id", "observation_sequence"),
        CheckConstraint("observation_sequence > 0", name="ck_attempt_observation_sequence"),
        CheckConstraint(
            "attempt_ordinal BETWEEN 0 AND 3",
            name="ck_attempt_observation_ordinal",
        ),
        CheckConstraint(
            "source IN ('DISPATCH_OUTCOME', 'TARGETED_LOOKUP', 'TARGETED_LOOKUP_FAILURE')",
            name="ck_attempt_observation_source",
        ),
        CheckConstraint(
            "(provider_present = true AND observed_payload IS NOT NULL) OR "
            "(provider_present = false AND observed_payload IS NULL)",
            name="ck_attempt_observation_presence",
        ),
    )

    observation_id: Mapped[UUID] = mapped_column(primary_key=True)
    permit_id: Mapped[UUID] = mapped_column(ForeignKey("broker_mutation_permits.permit_id"))
    execution_intent_id: Mapped[UUID] = mapped_column(ForeignKey("execution_intents.intent_id"))
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("order_attempts.attempt_id"))
    attempt_ordinal: Mapped[int] = mapped_column(Integer)
    observation_sequence: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32))
    provider_present: Mapped[bool] = mapped_column(Boolean)
    observed_payload: Mapped[dict[str, object] | None] = mapped_column(JSON_NULLABLE_DOCUMENT)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observation_hash: Mapped[str] = mapped_column(String(64), unique=True)


class AgentInputSnapshotRow(Base):
    __tablename__ = "agent_input_snapshots"
    __table_args__ = (
        CheckConstraint(
            "decision_kind IN ('OPPORTUNITY', 'ASSESSMENT')", name="ck_agent_input_kind"
        ),
        Index("ix_agent_input_boundary", "account_role", "decision_kind", "decision_boundary"),
        CheckConstraint(
            "observed_at >= decision_boundary", name="ck_agent_input_observation_boundary"
        ),
        CheckConstraint(
            "length(account_fingerprint) = 64 AND "
            "account_fingerprint NOT GLOB '*[^0-9a-f]*' AND "
            "length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_agent_input_hashes",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "account_fingerprint ~ '^[0-9a-f]{64}$' AND input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_agent_input_hashes",
        ).ddl_if(dialect="postgresql"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(primary_key=True)
    thesis_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("thesis_versions.thesis_version_id")
    )
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    decision_kind: Mapped[str] = mapped_column(String(16))
    decision_boundary: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    normalized_payload: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    input_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentDecisionRow(Base):
    __tablename__ = "agent_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision_kind IN ('OPPORTUNITY', 'ASSESSMENT')", name="ck_agent_decision_kind"
        ),
        CheckConstraint(
            "NOT (account_role = 'SUBMISSION' AND decision_kind = 'OPPORTUNITY') OR "
            "(outcome = 'NO_TRADE' AND reason_code = 'CALIBRATION_BINDING_NO_TRADE' "
            "AND autonomy_authorized = false) OR "
            "(outcome IN ('NO_TRADE', 'OPPORTUNITY_DECISION_PENDING', "
            "'PROVIDER_FAILURE_NO_TRADE') "
            "AND autonomy_authorized = false) OR thesis_version_id IS NOT NULL",
            name="ck_submission_opportunity_lineage",
        ),
        Index(
            "uq_agent_policy_decision_boundary",
            "account_role",
            "decision_kind",
            "decision_boundary",
            unique=True,
            sqlite_where=text(
                "outcome NOT IN ('OPPORTUNITY_DECISION_PENDING', "
                "'PROVIDER_FAILURE_NO_TRADE', 'PROVIDER_FAILURE_NO_ACTION')"
            ),
            postgresql_where=text(
                "outcome NOT IN ('OPPORTUNITY_DECISION_PENDING', "
                "'PROVIDER_FAILURE_NO_TRADE', 'PROVIDER_FAILURE_NO_ACTION')"
            ),
        ),
        CheckConstraint(
            "length(account_fingerprint) = 64 AND "
            "account_fingerprint NOT GLOB '*[^0-9a-f]*' AND "
            "length(policy_hash) = 64 AND policy_hash NOT GLOB '*[^0-9a-f]*' AND "
            "length(result_hash) = 64 AND result_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_agent_decision_hashes",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "account_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "policy_hash ~ '^[0-9a-f]{64}$' AND result_hash ~ '^[0-9a-f]{64}$'",
            name="ck_agent_decision_hashes",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(experiment_id IS NULL AND experiment_source_definition_hash IS NULL "
            "AND experiment_protocol_hash IS NULL) OR "
            "(experiment_id IS NOT NULL AND experiment_source_definition_hash IS NOT NULL "
            "AND experiment_protocol_hash IS NOT NULL)",
            name="ck_agent_decision_experiment_lineage",
        ),
        ForeignKeyConstraint(
            (
                "experiment_id",
                "experiment_source_definition_hash",
                "experiment_protocol_hash",
            ),
            (
                "compiled_experiment_versions.experiment_id",
                "compiled_experiment_versions.source_definition_hash",
                "compiled_experiment_versions.protocol_hash",
            ),
            name="fk_agent_decision_experiment_compiled",
            ondelete="RESTRICT",
            match="FULL",
        ),
        UniqueConstraint(
            "decision_id",
            "experiment_id",
            "experiment_source_definition_hash",
            "experiment_protocol_hash",
            name="uq_agent_decision_experiment_identity",
        ),
    )

    decision_id: Mapped[UUID] = mapped_column(primary_key=True)
    thesis_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("thesis_versions.thesis_version_id")
    )
    origin_tick_id: Mapped[UUID] = mapped_column(ForeignKey("agent_ticks.tick_id"))
    input_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_input_snapshots.snapshot_id"), unique=True
    )
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    decision_kind: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str] = mapped_column(String(48))
    reason_code: Mapped[str] = mapped_column(String(64))
    policy_hash: Mapped[str] = mapped_column(String(64))
    experiment_id: Mapped[UUID | None] = mapped_column()
    experiment_source_definition_hash: Mapped[str | None] = mapped_column(String(64))
    experiment_protocol_hash: Mapped[str | None] = mapped_column(String(64))
    result_payload: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    result_hash: Mapped[str] = mapped_column(String(64), unique=True)
    autonomy_authorized: Mapped[bool] = mapped_column(Boolean)
    decision_boundary: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentTickRow(Base):
    __tablename__ = "agent_ticks"
    __table_args__ = (
        CheckConstraint("actor IN ('OWNER', 'SCHEDULER')", name="ck_agent_tick_actor"),
        CheckConstraint("status IN ('RESERVED', 'COMPLETED')", name="ck_agent_tick_status"),
        CheckConstraint(
            "(status = 'RESERVED' AND terminal_code IS NULL AND proof_hash IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'COMPLETED' AND terminal_code IS NOT NULL AND proof_hash IS NOT NULL "
            "AND completed_at IS NOT NULL AND completed_at >= created_at)",
            name="ck_agent_tick_completion",
        ),
        CheckConstraint(
            "length(account_fingerprint) = 64 AND "
            "account_fingerprint NOT GLOB '*[^0-9a-f]*' AND "
            "(proof_hash IS NULL OR (length(proof_hash) = 64 "
            "AND proof_hash NOT GLOB '*[^0-9a-f]*'))",
            name="ck_agent_tick_hashes",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "account_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "(proof_hash IS NULL OR proof_hash ~ '^[0-9a-f]{64}$')",
            name="ck_agent_tick_hashes",
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint("account_role", "tick_key", name="uq_agent_tick_key"),
    )

    tick_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    tick_key: Mapped[str] = mapped_column(String(128))
    tick_boundary: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    reservation_token: Mapped[UUID] = mapped_column(unique=True)
    terminal_code: Mapped[str | None] = mapped_column(String(64))
    decision_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_decisions.decision_id"))
    execution_certificate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("execution_certificates.certificate_id")
    )
    proof_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EntryApprovalCertificateRow(Base):
    __tablename__ = "entry_approval_certificates"
    __table_args__ = (
        CheckConstraint("approved_max_loss > 0", name="ck_entry_approval_positive_risk"),
        CheckConstraint(
            f"quantity BETWEEN 1 AND {MAX_STRUCTURAL_OPTION_QUANTITY}",
            name="ck_entry_approval_quantity",
        ),
        CheckConstraint("expires_at > valid_from", name="ck_entry_approval_validity"),
        CheckConstraint(
            "(experiment_id IS NULL AND experiment_source_definition_hash IS NULL "
            "AND experiment_protocol_hash IS NULL) OR "
            "(experiment_id IS NOT NULL AND agent_decision_id IS NOT NULL "
            "AND experiment_source_definition_hash IS NOT NULL "
            "AND experiment_protocol_hash IS NOT NULL)",
            name="ck_entry_approval_experiment_lineage",
        ),
        ForeignKeyConstraint(
            (
                "agent_decision_id",
                "experiment_id",
                "experiment_source_definition_hash",
                "experiment_protocol_hash",
            ),
            (
                "agent_decisions.decision_id",
                "agent_decisions.experiment_id",
                "agent_decisions.experiment_source_definition_hash",
                "agent_decisions.experiment_protocol_hash",
            ),
            name="fk_entry_approval_experiment_decision",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "approval_id",
            "experiment_id",
            "experiment_source_definition_hash",
            "experiment_protocol_hash",
            name="uq_entry_approval_experiment_identity",
        ),
    )

    approval_id: Mapped[UUID] = mapped_column(primary_key=True)
    thesis_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("thesis_versions.thesis_version_id"), nullable=False
    )
    agent_decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_decisions.decision_id"), unique=True
    )
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    policy_hash: Mapped[str] = mapped_column(String(64))
    experiment_id: Mapped[UUID | None] = mapped_column()
    experiment_source_definition_hash: Mapped[str | None] = mapped_column(String(64))
    experiment_protocol_hash: Mapped[str | None] = mapped_column(String(64))
    book_fingerprint: Mapped[str] = mapped_column(String(64))
    envelope_hash: Mapped[str] = mapped_column(String(64))
    approved_max_loss: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    quantity: Mapped[int] = mapped_column(Integer)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid: Mapped[bool] = mapped_column(Boolean, default=True)


class ThesisVersionRow(Base):
    __tablename__ = "thesis_versions"
    __table_args__ = (
        UniqueConstraint("account_role", "version"),
        UniqueConstraint("thesis_id", "version"),
        CheckConstraint("version > 0", name="ck_thesis_version_positive"),
        CheckConstraint("target_at > frozen_at", name="ck_thesis_target_after_freeze"),
        CheckConstraint(
            f"approved_max_loss > 0 AND approved_max_loss <= {MAX_STRUCTURAL_APPROVED_RISK}",
            name="ck_thesis_approved_risk",
        ),
        CheckConstraint(
            f"portfolio_risk_cap > 0 AND portfolio_risk_cap <= {MAX_STRUCTURAL_LIFETIME_RISK}",
            name="ck_thesis_portfolio_risk",
        ),
    )

    thesis_version_id: Mapped[UUID] = mapped_column(primary_key=True)
    thesis_id: Mapped[UUID] = mapped_column()
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    version: Mapped[int] = mapped_column(Integer)
    origin_hash: Mapped[str] = mapped_column(String(64), unique=True)
    thesis_hash: Mapped[str] = mapped_column(String(64), unique=True)
    policy_hash: Mapped[str] = mapped_column(String(64))
    underlying: Mapped[str] = mapped_column(String(6))
    thesis_code: Mapped[str] = mapped_column(String(64))
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    target_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    intended_exposure: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    exposure_limits: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    volatility_view: Mapped[str] = mapped_column(String(16))
    entry_atm_iv: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    approved_max_loss: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    portfolio_risk_cap: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    invalidation_codes: Mapped[list[str]] = mapped_column(JSON_DOCUMENT)
    thesis_payload: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GreekAuthorityVersionRow(Base):
    __tablename__ = "greek_authority_versions"

    authority_id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timestamp_contract_hash: Mapped[str] = mapped_column(String(64), unique=True)
    units_contract_hash: Mapped[str] = mapped_column(String(64), unique=True)
    authority_payload: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    authority_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DevelopmentOpportunityPlanRow(Base):
    __tablename__ = "development_opportunity_plans"
    __table_args__ = (
        UniqueConstraint(
            "account_role",
            "opportunity_key",
            "version",
            name="uq_development_opportunity_plan",
        ),
        CheckConstraint(
            "account_role IN ('DEVELOPMENT', 'SUBMISSION')",
            name="ck_opportunity_plan_role",
        ),
        CheckConstraint("version > 0", name="ck_opportunity_plan_version"),
        CheckConstraint("benchmark_symbol = 'QQQ'", name="ck_opportunity_plan_benchmark"),
        CheckConstraint(
            "daily_start_session < pre_event_session "
            "AND pre_event_session < event_session AND event_session < reaction_session "
            "AND reaction_session <= signal_session",
            name="ck_opportunity_plan_sessions",
        ),
        CheckConstraint(
            "frozen_at <= evidence_window_start AND evidence_window_start <= evidence_window_end",
            name="ck_opportunity_plan_evidence_window",
        ),
        CheckConstraint(
            "json_array_length(allowed_event_codes) BETWEEN 1 AND 12",
            name="ck_opportunity_plan_event_codes",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "jsonb_array_length(allowed_event_codes) BETWEEN 1 AND 12",
            name="ck_opportunity_plan_event_codes",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "json_array_length(invalidation_codes) > 0",
            name="ck_opportunity_plan_invalidations",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "jsonb_array_length(invalidation_codes) > 0",
            name="ck_opportunity_plan_invalidations",
        ).ddl_if(dialect="postgresql"),
    )

    plan_id: Mapped[UUID] = mapped_column(primary_key=True)
    opportunity_key: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer)
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    underlying: Mapped[str] = mapped_column(String(6))
    benchmark_symbol: Mapped[str] = mapped_column(String(6))
    event_session: Mapped[date] = mapped_column(Date)
    pre_event_session: Mapped[date] = mapped_column(Date)
    reaction_session: Mapped[date] = mapped_column(Date)
    signal_session: Mapped[date] = mapped_column(Date)
    daily_start_session: Mapped[date] = mapped_column(Date)
    allowed_event_codes: Mapped[list[str]] = mapped_column(JSON_DOCUMENT)
    evidence_window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence_window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    policy_payload: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    policy_hash: Mapped[str] = mapped_column(String(64))
    request_contract: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    request_contract_hash: Mapped[str] = mapped_column(String(64))
    thesis_code: Mapped[str] = mapped_column(String(64))
    thesis_target_contract: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    thesis_target_hash: Mapped[str] = mapped_column(String(64))
    exposure_limit_contract: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    exposure_limit_hash: Mapped[str] = mapped_column(String(64))
    invalidation_codes: Mapped[list[str]] = mapped_column(JSON_DOCUMENT)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    plan_material: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    plan_hash: Mapped[str] = mapped_column(String(64), unique=True)


class DevelopmentOpportunityBaselineRow(Base):
    __tablename__ = "development_opportunity_baselines"
    __table_args__ = (
        CheckConstraint(
            "account_role IN ('DEVELOPMENT', 'SUBMISSION')",
            name="ck_opportunity_baseline_role",
        ),
        CheckConstraint(
            "(account_role = 'DEVELOPMENT' AND submission_baseline_id IS NULL) OR "
            "(account_role = 'SUBMISSION' AND submission_baseline_id IS NOT NULL)",
            name="ck_opportunity_submission_baseline_binding",
        ),
        CheckConstraint(
            "positions_complete AND orders_complete AND activity_complete",
            name="ck_opportunity_baseline_complete",
        ),
    )

    baseline_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("development_opportunity_plans.plan_id"), unique=True
    )
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    submission_baseline_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("submission_baselines.baseline_id")
    )
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    account_source_hash: Mapped[str] = mapped_column(String(64))
    positions_manifest: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    positions_source_hash: Mapped[str] = mapped_column(String(64))
    positions_complete: Mapped[bool] = mapped_column(Boolean)
    orders_manifest: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    orders_source_hash: Mapped[str] = mapped_column(String(64))
    orders_complete: Mapped[bool] = mapped_column(Boolean)
    activity_manifest: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    activity_source_hash: Mapped[str] = mapped_column(String(64))
    activity_complete: Mapped[bool] = mapped_column(Boolean)
    book_hash: Mapped[str] = mapped_column(String(64))
    history_hash: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    baseline_material: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    baseline_hash: Mapped[str] = mapped_column(String(64), unique=True)


class OpportunityObservationManifestRow(Base):
    __tablename__ = "opportunity_observation_manifests"
    __table_args__ = (
        CheckConstraint(
            "account_role IN ('DEVELOPMENT', 'SUBMISSION')",
            name="ck_opportunity_manifest_role",
        ),
        CheckConstraint("evaluated_at >= trusted_at", name="ck_opportunity_manifest_chronology"),
    )

    observation_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("development_opportunity_plans.plan_id"), unique=True
    )
    baseline_id: Mapped[UUID] = mapped_column(
        ForeignKey("development_opportunity_baselines.baseline_id"), unique=True
    )
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    policy_hash: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    calendar_hash: Mapped[str] = mapped_column(String(64))
    daily_hash: Mapped[str] = mapped_column(String(64))
    intraday_hash: Mapped[str] = mapped_column(String(64))
    signal_authority_hash: Mapped[str] = mapped_column(String(64))
    halt_hash: Mapped[str] = mapped_column(String(64))
    catalyst_hash: Mapped[str] = mapped_column(String(64))
    greek_hash: Mapped[str] = mapped_column(String(64))
    account_hash: Mapped[str] = mapped_column(String(64))
    activity_hash: Mapped[str] = mapped_column(String(64))
    budget_hash: Mapped[str] = mapped_column(String(64))
    prior_decision_hash: Mapped[str] = mapped_column(String(64))
    trusted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observation_material: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)


class AlpacaMarketSessionRow(Base):
    __tablename__ = "alpaca_market_sessions"
    __table_args__ = (CheckConstraint("close_at > open_at", name="ck_alpaca_session_window"),)

    market_session_id: Mapped[UUID] = mapped_column(primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, unique=True)
    open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    close_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_hash: Mapped[str] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_payload: Mapped[dict[str, object] | None] = mapped_column(JSON_NULLABLE_DOCUMENT)
    session_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AssessmentCertificateRow(Base):
    __tablename__ = "assessment_certificates"
    __table_args__ = (
        CheckConstraint("action IN ('CLOSE', 'ROLL')", name="ck_assessment_action"),
        CheckConstraint("approved_max_loss > 0", name="ck_assessment_positive_risk"),
        CheckConstraint(
            f"quantity BETWEEN 1 AND {MAX_STRUCTURAL_OPTION_QUANTITY}",
            name="ck_assessment_quantity",
        ),
        CheckConstraint("expires_at > created_at", name="ck_assessment_validity"),
        CheckConstraint(
            "(experiment_id IS NULL AND experiment_source_definition_hash IS NULL "
            "AND experiment_protocol_hash IS NULL) OR "
            "(experiment_id IS NOT NULL AND agent_decision_id IS NOT NULL "
            "AND experiment_source_definition_hash IS NOT NULL "
            "AND experiment_protocol_hash IS NOT NULL)",
            name="ck_assessment_experiment_lineage",
        ),
        ForeignKeyConstraint(
            (
                "agent_decision_id",
                "experiment_id",
                "experiment_source_definition_hash",
                "experiment_protocol_hash",
            ),
            (
                "agent_decisions.decision_id",
                "agent_decisions.experiment_id",
                "agent_decisions.experiment_source_definition_hash",
                "agent_decisions.experiment_protocol_hash",
            ),
            name="fk_assessment_experiment_decision",
            ondelete="RESTRICT",
        ),
    )

    certificate_id: Mapped[UUID] = mapped_column(primary_key=True)
    thesis_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("thesis_versions.thesis_version_id"), nullable=False
    )
    agent_decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_decisions.decision_id"), unique=True
    )
    assessment_id: Mapped[UUID] = mapped_column(unique=True)
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    action: Mapped[str] = mapped_column(String(8))
    position_fingerprint: Mapped[str] = mapped_column(String(64))
    envelope_hash: Mapped[str] = mapped_column(String(64))
    approved_max_loss: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    quantity: Mapped[int] = mapped_column(Integer)
    expected_after_exposure: Mapped[dict[str, object] | None] = mapped_column(JSON_DOCUMENT)
    policy_hash: Mapped[str] = mapped_column(String(64))
    experiment_id: Mapped[UUID | None] = mapped_column()
    experiment_source_definition_hash: Mapped[str | None] = mapped_column(String(64))
    experiment_protocol_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid: Mapped[bool] = mapped_column(Boolean, default=True)


class ExecutionIntentRow(Base):
    __tablename__ = "execution_intents"
    __table_args__ = (
        Index(
            "ix_entry_event_day",
            "account_role",
            "event_key",
            "trading_day",
            unique=False,
            postgresql_where=text("action = 'ENTRY'"),
            sqlite_where=text("action = 'ENTRY'"),
        ),
        Index(
            "uq_active_intent_per_account",
            "account_role",
            unique=True,
            postgresql_where=text("action = 'ENTRY' AND state <> 'TERMINAL'"),
            sqlite_where=text("action = 'ENTRY' AND state <> 'TERMINAL'"),
        ),
        Index(
            "uq_claimed_execution_lease",
            "account_role",
            unique=True,
            postgresql_where=text("state = 'CLAIMED'"),
            sqlite_where=text("state = 'CLAIMED'"),
        ),
        Index(
            "uq_roll_intent_per_position_session",
            "account_role",
            "fingerprint",
            "market_session_id",
            unique=True,
            postgresql_where=text("action = 'ROLL'"),
            sqlite_where=text("action = 'ROLL'"),
        ),
        CheckConstraint(
            "(action = 'ENTRY' AND entry_approval_id IS NOT NULL "
            "AND assessment_certificate_id IS NULL) OR "
            "(action IN ('CLOSE', 'ROLL') AND entry_approval_id IS NULL "
            "AND assessment_certificate_id IS NOT NULL)",
            name="ck_execution_intent_authorization_origin",
        ),
        CheckConstraint("action IN ('ENTRY', 'CLOSE', 'ROLL')", name="ck_intent_action"),
        CheckConstraint(
            f"quantity BETWEEN 1 AND {MAX_STRUCTURAL_OPTION_QUANTITY}",
            name="ck_intent_quantity",
        ),
        CheckConstraint("minimum_limit <= maximum_limit", name="ck_intent_price_envelope"),
        CheckConstraint(
            f"approved_max_loss > 0 AND approved_max_loss <= {MAX_STRUCTURAL_APPROVED_RISK}",
            name="ck_intent_approved_risk",
        ),
        CheckConstraint(
            "(action = 'ROLL' AND market_session_id IS NOT NULL "
            "AND quoted_relative_spread IS NOT NULL AND quoted_relative_spread >= 0 "
            "AND maximum_relative_spread IS NOT NULL "
            "AND maximum_relative_spread >= quoted_relative_spread "
            "AND maximum_relative_spread < 1 "
            "AND incremental_debit IS NOT NULL AND incremental_debit >= 0 "
            "AND maximum_incremental_debit IS NOT NULL "
            "AND maximum_incremental_debit >= incremental_debit "
            "AND maximum_incremental_debit <= approved_max_loss) OR "
            "(action <> 'ROLL' AND market_session_id IS NULL "
            "AND quoted_relative_spread IS NULL AND maximum_relative_spread IS NULL "
            "AND incremental_debit IS NULL AND maximum_incremental_debit IS NULL)",
            name="ck_intent_roll_authority",
        ),
        CheckConstraint("state IN ('APPROVED', 'CLAIMED', 'TERMINAL')", name="ck_intent_state"),
        CheckConstraint(
            "claim_generation >= 0 AND execution_epoch >= 0 AND "
            "(state <> 'CLAIMED' OR (claim_token IS NOT NULL AND claim_generation > 0 "
            "AND heartbeat_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND claimed_at IS NOT NULL AND heartbeat_at >= claimed_at "
            "AND lease_expires_at > heartbeat_at))",
            name="ck_intent_claim_fence",
        ),
    )

    intent_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    intent_digest: Mapped[str] = mapped_column(String(64), unique=True)
    action: Mapped[str] = mapped_column(String(8))
    policy_hash: Mapped[str] = mapped_column(String(64))
    event_key: Mapped[str] = mapped_column(String(80))
    trading_day: Mapped[date] = mapped_column(Date)
    entry_approval_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("entry_approval_certificates.approval_id")
    )
    assessment_certificate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assessment_certificates.certificate_id")
    )
    fingerprint: Mapped[str] = mapped_column(String(64))
    envelope_hash: Mapped[str] = mapped_column(String(64))
    envelope_payload: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    legs: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    quantity: Mapped[int] = mapped_column(Integer)
    minimum_limit: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    maximum_limit: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    approved_max_loss: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    market_session_id: Mapped[UUID | None] = mapped_column()
    quoted_relative_spread: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    maximum_relative_spread: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    incremental_debit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    maximum_incremental_debit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    state: Mapped[str] = mapped_column(String(24))
    claimed_by: Mapped[str | None] = mapped_column(String(16))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(unique=True)
    claim_generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    execution_epoch: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_fill_consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class OrderAttemptRow(Base):
    __tablename__ = "order_attempts"
    __table_args__ = (
        UniqueConstraint("execution_intent_id", "attempt_ordinal"),
        CheckConstraint("attempt_ordinal BETWEEN 0 AND 3", name="ck_attempt_ordinal"),
        CheckConstraint(
            f"quantity BETWEEN 0 AND {MAX_STRUCTURAL_OPTION_QUANTITY}",
            name="ck_attempt_quantity",
        ),
        CheckConstraint(
            "filled_quantity BETWEEN 0 AND quantity", name="ck_attempt_filled_quantity"
        ),
        CheckConstraint(
            "(quote_hash IS NULL AND json_array_length(quote_source_timestamps) = 0 "
            "AND quote_retrieved_at IS NULL AND timing_authority_at IS NULL "
            "AND prior_request_hash IS NULL) OR "
            "(attempt_ordinal > 0 AND length(quote_hash) = 64 "
            "AND quote_hash NOT GLOB '*[^0-9a-f]*' "
            "AND json_array_length(quote_source_timestamps) > 0 "
            "AND quote_retrieved_at IS NOT NULL AND timing_authority_at IS NOT NULL "
            "AND length(prior_request_hash) = 64 "
            "AND prior_request_hash NOT GLOB '*[^0-9a-f]*')",
            name="ck_order_attempt_quote_authority",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "(quote_hash IS NULL AND jsonb_array_length(quote_source_timestamps) = 0 "
            "AND quote_retrieved_at IS NULL AND timing_authority_at IS NULL "
            "AND prior_request_hash IS NULL) OR "
            "(attempt_ordinal > 0 AND quote_hash ~ '^[0-9a-f]{64}$' "
            "AND jsonb_array_length(quote_source_timestamps) > 0 "
            "AND quote_retrieved_at IS NOT NULL AND timing_authority_at IS NOT NULL "
            "AND prior_request_hash ~ '^[0-9a-f]{64}$')",
            name="ck_order_attempt_quote_authority",
        ).ddl_if(dialect="postgresql"),
    )

    attempt_id: Mapped[UUID] = mapped_column(primary_key=True)
    broker_permit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("broker_mutation_permits.permit_id"), unique=True
    )
    execution_intent_id: Mapped[UUID] = mapped_column(ForeignKey("execution_intents.intent_id"))
    attempt_ordinal: Mapped[int] = mapped_column(Integer)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    provider_order_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    state: Mapped[str] = mapped_column(String(40))
    request_hash: Mapped[str] = mapped_column(String(64))
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    quote_hash: Mapped[str | None] = mapped_column(String(64))
    quote_source_timestamps: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list)
    quote_retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timing_authority_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prior_request_hash: Mapped[str | None] = mapped_column(String(64))
    replaces_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("order_attempts.attempt_id")
    )
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    fill_cash_flow: Mapped[Decimal | None] = mapped_column("filled_cash_flow", Numeric(18, 6))


class ExecutionCertificateRow(Base):
    __tablename__ = "execution_certificates"
    __table_args__ = (
        CheckConstraint(
            "(entry_approval_id IS NULL) <> (assessment_certificate_id IS NULL)",
            name="ck_execution_certificate_authorization_origin",
        ),
        CheckConstraint(
            "(reconciliation_id IS NULL AND reconciliation_hash IS NULL "
            "AND last_observation_hash IS NULL) OR "
            "(reconciliation_id IS NOT NULL AND reconciliation_hash IS NOT NULL "
            "AND last_observation_hash IS NOT NULL)",
            name="ck_execution_certificate_reconciliation_provenance",
        ),
    )

    certificate_id: Mapped[UUID] = mapped_column(primary_key=True)
    execution_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_intents.intent_id"), unique=True
    )
    entry_approval_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("entry_approval_certificates.approval_id")
    )
    assessment_certificate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assessment_certificates.certificate_id")
    )
    execution_status: Mapped[str] = mapped_column(String(48))
    attempt_ids: Mapped[list[str]] = mapped_column(JSON_DOCUMENT)
    actual_exposure: Mapped[dict[str, object] | None] = mapped_column(JSON_DOCUMENT)
    reconciliation_checks: Mapped[list[str]] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reconciliation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("whole_account_reconciliations.reconciliation_id"), unique=True
    )
    reconciliation_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    last_observation_hash: Mapped[str | None] = mapped_column(
        ForeignKey("attempt_observations.observation_hash"), unique=True
    )


class ManagedLifecyclePositionRow(Base):
    __tablename__ = "managed_lifecycle_positions"
    __table_args__ = (
        Index(
            "uq_active_managed_position_role",
            "account_role",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
            sqlite_where=text("closed_at IS NULL"),
        ),
        CheckConstraint(
            "(experiment_id IS NULL AND experiment_source_definition_hash IS NULL "
            "AND experiment_protocol_hash IS NULL) OR "
            "(experiment_id IS NOT NULL AND experiment_source_definition_hash IS NOT NULL "
            "AND experiment_protocol_hash IS NOT NULL)",
            name="ck_managed_position_experiment_lineage",
        ),
        ForeignKeyConstraint(
            (
                "entry_approval_id",
                "experiment_id",
                "experiment_source_definition_hash",
                "experiment_protocol_hash",
            ),
            (
                "entry_approval_certificates.approval_id",
                "entry_approval_certificates.experiment_id",
                "entry_approval_certificates.experiment_source_definition_hash",
                "entry_approval_certificates.experiment_protocol_hash",
            ),
            name="fk_managed_position_experiment_approval",
            ondelete="RESTRICT",
        ),
    )

    managed_position_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    entry_execution_certificate_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_certificates.certificate_id"), unique=True
    )
    entry_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_intents.intent_id"), unique=True
    )
    entry_approval_id: Mapped[UUID] = mapped_column(
        ForeignKey("entry_approval_certificates.approval_id"), unique=True
    )
    thesis_version_id: Mapped[UUID] = mapped_column(ForeignKey("thesis_versions.thesis_version_id"))
    experiment_id: Mapped[UUID | None] = mapped_column()
    experiment_source_definition_hash: Mapped[str | None] = mapped_column(String(64))
    experiment_protocol_hash: Mapped[str | None] = mapped_column(String(64))
    entry_reconciliation_id: Mapped[UUID] = mapped_column(
        ForeignKey("whole_account_reconciliations.reconciliation_id"), unique=True
    )
    current_reconciliation_state_id: Mapped[UUID] = mapped_column(
        ForeignKey("account_reconciliation_states.state_id")
    )
    current_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("managed_position_snapshots.snapshot_id")
    )
    active_position_fingerprint: Mapped[str] = mapped_column(String(64))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ManagedPositionTransitionRow(Base):
    __tablename__ = "managed_position_transitions"
    __table_args__ = (
        UniqueConstraint("managed_position_id", "transition_sequence"),
        CheckConstraint("transition_sequence >= 0", name="ck_managed_transition_sequence"),
        CheckConstraint("action IN ('ENTRY', 'ROLL', 'CLOSE')", name="ck_managed_action"),
        CheckConstraint(
            "abs(cashflow_contribution) <= 1000000000",
            name="ck_managed_cashflow_bound",
        ),
    )

    transition_id: Mapped[UUID] = mapped_column(primary_key=True)
    managed_position_id: Mapped[UUID] = mapped_column(
        ForeignKey("managed_lifecycle_positions.managed_position_id")
    )
    predecessor_transition_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("managed_position_transitions.transition_id"), unique=True
    )
    transition_sequence: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(8))
    execution_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_intents.intent_id"), unique=True
    )
    execution_certificate_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_certificates.certificate_id"), unique=True
    )
    post_reconciliation_id: Mapped[UUID] = mapped_column(
        ForeignKey("whole_account_reconciliations.reconciliation_id"), unique=True
    )
    fill_activity_manifest: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    fill_activity_manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)
    cashflow_contribution: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    resulting_position_fingerprint: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    market_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("alpaca_market_sessions.market_session_id")
    )
    transition_hash: Mapped[str] = mapped_column(String(64), unique=True)


class ManagedPositionSnapshotRow(Base):
    __tablename__ = "managed_position_snapshots"
    __table_args__ = (
        CheckConstraint(
            "rolls_on_trading_day BETWEEN 0 AND 64",
            name="ck_managed_daily_rolls",
        ),
        CheckConstraint(
            "abs(cumulative_cashflow) <= 1000000000",
            name="ck_managed_cumulative_cashflow",
        ),
    )

    snapshot_id: Mapped[UUID] = mapped_column(primary_key=True)
    managed_position_id: Mapped[UUID] = mapped_column(
        ForeignKey("managed_lifecycle_positions.managed_position_id")
    )
    predecessor_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("managed_position_snapshots.snapshot_id"), unique=True
    )
    transition_id: Mapped[UUID] = mapped_column(
        ForeignKey("managed_position_transitions.transition_id"), unique=True
    )
    reconciliation_id: Mapped[UUID] = mapped_column(
        ForeignKey("whole_account_reconciliations.reconciliation_id"), unique=True
    )
    reconciliation_state_id: Mapped[UUID] = mapped_column(
        ForeignKey("account_reconciliation_states.state_id")
    )
    normalized_inventory: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    inventory_hash: Mapped[str] = mapped_column(String(64))
    activity_manifest: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    activity_manifest_hash: Mapped[str] = mapped_column(String(64))
    cumulative_cashflow: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    rolls_on_trading_day: Mapped[int] = mapped_column(Integer)
    market_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("alpaca_market_sessions.market_session_id")
    )
    position_fingerprint: Mapped[str] = mapped_column(String(64))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    snapshot_hash: Mapped[str] = mapped_column(String(64), unique=True)


class LifecycleObservationManifestRow(Base):
    __tablename__ = "lifecycle_observation_manifests"

    manifest_id: Mapped[UUID] = mapped_column(primary_key=True)
    manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)
    agent_input_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_input_snapshots.snapshot_id"), unique=True
    )
    account_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("lifecycle_account_observations.observation_id"), unique=True
    )
    managed_position_id: Mapped[UUID] = mapped_column(
        ForeignKey("managed_lifecycle_positions.managed_position_id")
    )
    managed_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("managed_position_snapshots.snapshot_id")
    )
    reconciliation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("whole_account_reconciliations.reconciliation_id")
    )
    greek_authority_id: Mapped[UUID] = mapped_column(
        ForeignKey("greek_authority_versions.authority_id")
    )
    sweep_hash: Mapped[str] = mapped_column(String(64))
    account_manifest: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    activity_manifest: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    option_manifest: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    atm_iv_manifest: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    underlying_manifest: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    boundary_manifest: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    research_manifest: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    source_authority_manifest: Mapped[list[dict[str, object]] | None] = mapped_column(
        JSON_NULLABLE_DOCUMENT
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LifecycleLaunchAuthorityRow(Base):
    __tablename__ = "lifecycle_launch_authorities"

    managed_position_id: Mapped[UUID] = mapped_column(
        ForeignKey("managed_lifecycle_positions.managed_position_id"), primary_key=True
    )
    beta60: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    benchmark_symbol: Mapped[str] = mapped_column(String(6))
    entry_boundary_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_policy_hash: Mapped[str] = mapped_column(String(64))
    underlying_source_hash: Mapped[str] = mapped_column(String(64))
    benchmark_source_hash: Mapped[str] = mapped_column(String(64))
    completed_bar_source_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EntryMaterializationJobRow(Base):
    __tablename__ = "entry_materialization_jobs"

    execution_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_intents.intent_id"), primary_key=True
    )
    entry_approval_id: Mapped[UUID] = mapped_column(
        ForeignKey("entry_approval_certificates.approval_id"), unique=True
    )
    account_role: Mapped[str] = mapped_column(String(16))
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    beta60: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    benchmark_symbol: Mapped[str] = mapped_column(String(6))
    entry_boundary_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_policy_hash: Mapped[str] = mapped_column(String(64))
    underlying_source_hash: Mapped[str] = mapped_column(String(64))
    benchmark_source_hash: Mapped[str] = mapped_column(String(64))
    completed_bar_source_hash: Mapped[str] = mapped_column(String(64))
    job_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    managed_position_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("managed_lifecycle_positions.managed_position_id"), unique=True
    )
    terminal_status: Mapped[str | None] = mapped_column(String(40))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LifecycleSourceObservationRow(Base):
    __tablename__ = "lifecycle_source_observations"
    __table_args__ = (
        CheckConstraint(
            "account_role IS NULL OR account_role IN ('DEVELOPMENT', 'SUBMISSION')",
            name="ck_lifecycle_source_role",
        ),
    )

    source_id: Mapped[UUID] = mapped_column(primary_key=True)
    external_source_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    source_kind: Mapped[str] = mapped_column(String(32))
    account_role: Mapped[str | None] = mapped_column(String(16))
    account_fingerprint: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    result_hash: Mapped[str] = mapped_column(String(64))
    normalized_payload: Mapped[dict[str, object] | list[object]] = mapped_column(JSON_DOCUMENT)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LifecycleAccountObservationRow(Base):
    __tablename__ = "lifecycle_account_observations"
    __table_args__ = (
        CheckConstraint(
            "account_role IN ('DEVELOPMENT', 'SUBMISSION')",
            name="ck_lifecycle_account_role",
        ),
    )

    observation_id: Mapped[UUID] = mapped_column(primary_key=True)
    managed_position_id: Mapped[UUID] = mapped_column(
        ForeignKey("managed_lifecycle_positions.managed_position_id")
    )
    managed_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("managed_position_snapshots.snapshot_id")
    )
    account_role: Mapped[str] = mapped_column(String(16))
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    sweep_payload: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    sweep_hash: Mapped[str] = mapped_column(String(64), unique=True)
    retrieval_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retrieval_completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LifecycleObservationBindingRow(Base):
    __tablename__ = "lifecycle_observation_bindings"

    binding_id: Mapped[UUID] = mapped_column(primary_key=True)
    manifest_id: Mapped[UUID] = mapped_column(
        ForeignKey("lifecycle_observation_manifests.manifest_id"), unique=True
    )
    agent_input_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_input_snapshots.snapshot_id"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CompetitionEntryBudgetRow(Base):
    __tablename__ = "competition_entry_budget"
    __table_args__ = (
        CheckConstraint(
            f"entries_used BETWEEN 0 AND {MAX_STRUCTURAL_LIFETIME_ENTRIES}",
            name="ck_entry_budget_count",
        ),
        CheckConstraint(
            f"gross_approved_risk BETWEEN 0 AND {MAX_STRUCTURAL_LIFETIME_RISK}",
            name="ck_entry_budget_risk",
        ),
        CheckConstraint("reserved_risk >= 0", name="ck_entry_budget_reservation"),
    )

    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"), primary_key=True)
    entries_used: Mapped[int] = mapped_column(Integer, default=0)
    gross_approved_risk: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    reserved_intent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("execution_intents.intent_id")
    )
    reserved_risk: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)


class PerformanceSnapshotRow(Base):
    __tablename__ = "competition_performance_snapshots"
    __table_args__ = (
        CheckConstraint("account_role = 'SUBMISSION'", name="ck_performance_submission_role"),
        CheckConstraint(
            "measurement_status IN ('COMPLETE', 'MISSING', 'UNKNOWN')",
            name="ck_performance_measurement_status",
        ),
        CheckConstraint(
            "failure_code IS NULL OR failure_code IN "
            "('CAPTURE_NOT_STARTED', 'PROVIDER_UNAVAILABLE', 'ACCOUNT_STATE_INCOMPLETE', "
            "'BASELINE_UNAVAILABLE', 'SCHEMA_INVALID')",
            name="ck_performance_failure_code",
        ),
        CheckConstraint(
            "baseline_status IN ('BASELINE_NOT_CAPTURED', 'BASELINE_UNKNOWN', "
            "'BASELINE_CLEAN', 'BASELINE_CONTAMINATED')",
            name="ck_performance_baseline_status",
        ),
        CheckConstraint("attempted_at >= scheduled_for", name="ck_performance_attempt_time"),
        CheckConstraint(
            "measured_at IS NULL OR measured_at >= attempted_at",
            name="ck_performance_measured_time",
        ),
        CheckConstraint(
            "(measurement_status = 'COMPLETE' AND failure_code IS NULL "
            "AND measured_at IS NOT NULL AND current_equity IS NOT NULL) OR "
            "(measurement_status <> 'COMPLETE' AND failure_code IS NOT NULL "
            "AND measured_at IS NULL AND current_equity IS NULL "
            "AND account_equity_change IS NULL AND account_equity_return_pct IS NULL "
            "AND lifecycle_cashflow IS NULL AND liquidation_pnl IS NULL)",
            name="ck_performance_measurement_coherence",
        ),
        CheckConstraint(
            "(baseline_status = 'BASELINE_CLEAN' AND measurement_status = 'COMPLETE' "
            "AND account_equity_change = current_equity - 100000 "
            "AND account_equity_return_pct * 1000 = account_equity_change) OR "
            "((baseline_status <> 'BASELINE_CLEAN' OR measurement_status <> 'COMPLETE') "
            "AND account_equity_change IS NULL AND account_equity_return_pct IS NULL)",
            name="ck_performance_baseline_math",
        ),
        CheckConstraint(
            "(measurement_status = 'COMPLETE' "
            "AND positions_manifest_hash IS NOT NULL AND orders_manifest_hash IS NOT NULL "
            "AND activities_manifest_hash IS NOT NULL) OR "
            "(measurement_status <> 'COMPLETE' "
            "AND positions_manifest_hash IS NULL AND orders_manifest_hash IS NULL "
            "AND activities_manifest_hash IS NULL)",
            name="ck_performance_private_manifest_coherence",
        ),
        CheckConstraint(
            "(submission_baseline_id IS NULL AND baseline_status = 'BASELINE_NOT_CAPTURED' "
            "AND measurement_status <> 'COMPLETE') OR "
            "(submission_baseline_id IS NOT NULL "
            "AND baseline_status IN ('BASELINE_CLEAN', 'BASELINE_CONTAMINATED'))",
            name="ck_performance_baseline_binding",
        ),
        CheckConstraint("length(snapshot_hash) = 64", name="ck_performance_snapshot_hash"),
        CheckConstraint(
            "account_fingerprint IS NULL OR length(account_fingerprint) = 64",
            name="ck_performance_account_fingerprint",
        ),
        CheckConstraint(
            "positions_manifest_hash IS NULL OR length(positions_manifest_hash) = 64",
            name="ck_performance_positions_manifest_hash",
        ),
        CheckConstraint(
            "orders_manifest_hash IS NULL OR length(orders_manifest_hash) = 64",
            name="ck_performance_orders_manifest_hash",
        ),
        CheckConstraint(
            "activities_manifest_hash IS NULL OR length(activities_manifest_hash) = 64",
            name="ck_performance_activities_manifest_hash",
        ),
        CheckConstraint(
            "((scheduled_for = '2026-09-04 14:30:00.000000') "
            "= (boundary_key = 'FINAL_2026-09-04T14:30:00Z'))",
            name="ck_performance_final_boundary_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "((scheduled_for = timestamptz '2026-09-04 14:30:00+00') "
            "= (boundary_key = 'FINAL_2026-09-04T14:30:00Z'))",
            name="ck_performance_final_boundary_postgresql",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "snapshot_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_performance_snapshot_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_performance_snapshot_hash_postgresql",
        ).ddl_if(dialect="postgresql"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(primary_key=True)
    submission_baseline_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("submission_baselines.baseline_id")
    )
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    boundary_key: Mapped[str] = mapped_column(String(80), unique=True)
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, unique=True
    )
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    measurement_status: Mapped[str] = mapped_column(String(16))
    failure_code: Mapped[str | None] = mapped_column(String(40))
    current_equity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    account_equity_change: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    account_equity_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(18, 9))
    lifecycle_cashflow: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    liquidation_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    baseline_status: Mapped[str] = mapped_column(String(32))
    point_payload: Mapped[dict[str, object]] = mapped_column(JSON_DOCUMENT)
    account_fingerprint: Mapped[str] = mapped_column(String(64))
    positions_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    orders_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    activities_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    snapshot_hash: Mapped[str] = mapped_column(String(64), unique=True)


class PerformancePublicationRow(Base):
    __tablename__ = "competition_performance_publications"
    __table_args__ = (
        CheckConstraint(
            "published_at >= boundary_scheduled_for", name="ck_performance_publication_time"
        ),
        CheckConstraint("length(projection_hash) = 64", name="ck_performance_projection_hash"),
        CheckConstraint("length(publication_hash) = 64", name="ck_performance_publication_hash"),
        CheckConstraint(
            "predecessor_hash IS NULL OR length(predecessor_hash) = 64",
            name="ck_performance_predecessor_hash",
        ),
        CheckConstraint(
            "projection_hash NOT GLOB '*[^0-9a-f]*' "
            "AND publication_hash NOT GLOB '*[^0-9a-f]*' "
            "AND (predecessor_hash IS NULL OR predecessor_hash NOT GLOB '*[^0-9a-f]*')",
            name="ck_performance_publication_hashes_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "projection_hash ~ '^[0-9a-f]{64}$' "
            "AND publication_hash ~ '^[0-9a-f]{64}$' "
            "AND (predecessor_hash IS NULL OR predecessor_hash ~ '^[0-9a-f]{64}$')",
            name="ck_performance_publication_hashes_postgresql",
        ).ddl_if(dialect="postgresql"),
        Index(
            "uq_competition_performance_predecessor",
            "predecessor_hash",
            unique=True,
            postgresql_where=text("predecessor_hash IS NOT NULL"),
            sqlite_where=text("predecessor_hash IS NOT NULL"),
        ),
    )

    publication_id: Mapped[UUID] = mapped_column(primary_key=True)
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("competition_performance_snapshots.snapshot_id"), unique=True
    )
    boundary_scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_text: Mapped[str] = mapped_column(Text)
    projection_hash: Mapped[str] = mapped_column(String(64))
    publication_hash: Mapped[str] = mapped_column(String(64), unique=True)
    predecessor_hash: Mapped[str | None] = mapped_column(String(64))


class CompetitionRecordPublicationRow(Base):
    __tablename__ = "competition_record_publications"
    __table_args__ = (
        CheckConstraint("record_kind IN ('NO_TRADE', 'POSITION')", name="ck_record_kind"),
        CheckConstraint("account_role = 'SUBMISSION'", name="ck_record_submission_role"),
        CheckConstraint(
            "length(public_record_id) = 64 AND public_record_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_record_public_id",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint("public_record_id ~ '^[0-9a-f]{64}$'", name="ck_record_public_id").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            "length(source_authority_hash) = 64 AND source_authority_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_record_source_hash",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "source_authority_hash ~ '^[0-9a-f]{64}$'", name="ck_record_source_hash"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(projection_hash) = 64 AND projection_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_record_projection_hash",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "projection_hash ~ '^[0-9a-f]{64}$'", name="ck_record_projection_hash"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(publication_hash) = 64 AND publication_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_record_publication_hash",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "publication_hash ~ '^[0-9a-f]{64}$'", name="ck_record_publication_hash"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "predecessor_hash IS NULL OR (length(predecessor_hash) = 64 "
            "AND predecessor_hash NOT GLOB '*[^0-9a-f]*')",
            name="ck_record_predecessor_hash",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "predecessor_hash IS NULL OR predecessor_hash ~ '^[0-9a-f]{64}$'",
            name="ck_record_predecessor_hash",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(record_kind = 'NO_TRADE' AND source_decision_id IS NOT NULL "
            "AND source_managed_position_id IS NULL) OR "
            "(record_kind = 'POSITION' AND source_decision_id IS NULL "
            "AND source_managed_position_id IS NOT NULL)",
            name="ck_record_source_kind",
        ),
        UniqueConstraint(
            "public_record_id", "source_authority_hash", name="uq_competition_record_version"
        ),
        Index(
            "uq_competition_record_predecessor",
            "predecessor_hash",
            unique=True,
            postgresql_where=text("predecessor_hash IS NOT NULL"),
            sqlite_where=text("predecessor_hash IS NOT NULL"),
        ),
    )

    publication_id: Mapped[UUID] = mapped_column(primary_key=True)
    record_kind: Mapped[str] = mapped_column(String(16))
    account_role: Mapped[str] = mapped_column(ForeignKey("account_roles.role"))
    source_decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_decisions.decision_id"), unique=True
    )
    source_managed_position_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("managed_lifecycle_positions.managed_position_id")
    )
    public_record_id: Mapped[str] = mapped_column(String(64))
    source_authority_hash: Mapped[str] = mapped_column(String(64), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, unique=True)
    payload_text: Mapped[str] = mapped_column(Text)
    projection_hash: Mapped[str] = mapped_column(String(64))
    publication_hash: Mapped[str] = mapped_column(String(64), unique=True)
    predecessor_hash: Mapped[str | None] = mapped_column(String(64))


class ModelCallBudgetRow(Base):
    __tablename__ = "model_call_budgets"
    __table_args__ = (
        CheckConstraint(
            "model = 'gemini-3.7-flash' AND hard_limit = 50",
            name="ck_model_call_budget_identity",
        ),
        CheckConstraint(
            "request_count BETWEEN 0 AND hard_limit",
            name="ck_model_call_budget_count",
        ),
    )

    model: Mapped[str] = mapped_column(String(40), primary_key=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    hard_limit: Mapped[int] = mapped_column(Integer, default=50)


class OwnerProviderSettingsRow(Base):
    __tablename__ = "owner_provider_settings"
    __table_args__ = (
        CheckConstraint(
            "singleton_id = 'owner-ai-provider' AND schema_version = 1",
            name="ck_owner_provider_settings_identity",
        ),
        CheckConstraint(
            "generation > 0",
            name="ck_owner_provider_settings_generation",
        ),
        CheckConstraint(
            "(active = true AND provider IN ('OWNER_GEMINI', "
            "'OWNER_OPENAI_COMPATIBLE') AND endpoint IS NOT NULL AND model IS NOT NULL "
            "AND credential_nonce IS NOT NULL AND credential_ciphertext IS NOT NULL) OR "
            "(active = false AND provider IS NULL AND endpoint IS NULL AND model IS NULL "
            "AND credential_nonce IS NULL AND credential_ciphertext IS NULL)",
            name="ck_owner_provider_settings_state",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_owner_provider_settings_timestamps",
        ),
        CheckConstraint(
            "credential_nonce IS NULL OR length(credential_nonce) = 12",
            name="ck_owner_provider_settings_nonce_length",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "credential_nonce IS NULL OR octet_length(credential_nonce) = 12",
            name="ck_owner_provider_settings_nonce_length_postgresql",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "credential_ciphertext IS NULL OR length(credential_ciphertext) BETWEEN 17 AND 16400",
            name="ck_owner_provider_settings_ciphertext_length",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "credential_ciphertext IS NULL OR "
            "octet_length(credential_ciphertext) BETWEEN 17 AND 16400",
            name="ck_owner_provider_settings_ciphertext_length_postgresql",
        ).ddl_if(dialect="postgresql"),
    )

    singleton_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(32))
    endpoint: Mapped[str | None] = mapped_column(String(2048))
    model: Mapped[str | None] = mapped_column(String(256))
    generation: Mapped[int] = mapped_column(BigInteger)
    credential_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    credential_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReviewedExperimentDefinitionRow(Base):
    __tablename__ = "reviewed_experiment_definitions"
    __table_args__ = (
        CheckConstraint("version = 1", name="ck_reviewed_experiment_version"),
        CheckConstraint(
            "lifecycle_state = 'REVIEWED'",
            name="ck_reviewed_experiment_lifecycle",
        ),
        CheckConstraint(
            "definition_hash NOT GLOB '*[^0-9a-f]*' AND length(definition_hash) = 64",
            name="ck_reviewed_experiment_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "definition_hash ~ '^[0-9a-f]{64}$'",
            name="ck_reviewed_experiment_hash_postgresql",
        ).ddl_if(dialect="postgresql"),
    )

    experiment_id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    definition_hash: Mapped[str] = mapped_column(String(64), unique=True)
    lifecycle_state: Mapped[str] = mapped_column(String(16))
    payload_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class CompiledExperimentVersionRow(Base):
    __tablename__ = "compiled_experiment_versions"
    __table_args__ = (
        CheckConstraint("source_version = 1", name="ck_compiled_experiment_source_version"),
        CheckConstraint("compiled_version = 1", name="ck_compiled_experiment_version"),
        CheckConstraint(
            "lifecycle_state = 'COMPILED'",
            name="ck_compiled_experiment_lifecycle",
        ),
        CheckConstraint("arm_state = 'NOT_ARMED'", name="ck_compiled_experiment_arm"),
        CheckConstraint("automation_state = 'OFF'", name="ck_compiled_experiment_automation"),
        CheckConstraint(
            "execution_eligible = false",
            name="ck_compiled_experiment_execution",
        ),
        CheckConstraint(
            "source_definition_hash NOT GLOB '*[^0-9a-f]*' AND length(source_definition_hash) = 64",
            name="ck_compiled_experiment_source_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "source_definition_hash ~ '^[0-9a-f]{64}$'",
            name="ck_compiled_experiment_source_hash_postgresql",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "protocol_hash NOT GLOB '*[^0-9a-f]*' AND length(protocol_hash) = 64",
            name="ck_compiled_experiment_protocol_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "protocol_hash ~ '^[0-9a-f]{64}$'",
            name="ck_compiled_experiment_protocol_hash_postgresql",
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint(
            "experiment_id",
            "source_definition_hash",
            "protocol_hash",
            name="uq_compiled_experiment_authorization_identity",
        ),
    )

    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("reviewed_experiment_definitions.experiment_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    source_version: Mapped[int] = mapped_column(Integer)
    compiled_version: Mapped[int] = mapped_column(Integer)
    source_definition_hash: Mapped[str] = mapped_column(String(64))
    protocol_hash: Mapped[str] = mapped_column(String(64))
    lifecycle_state: Mapped[str] = mapped_column(String(16))
    arm_state: Mapped[str] = mapped_column(String(16))
    automation_state: Mapped[str] = mapped_column(String(16))
    execution_eligible: Mapped[bool] = mapped_column(Boolean)
    payload_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExperimentArmStateRow(Base):
    __tablename__ = "experiment_arm_states"
    __table_args__ = (
        CheckConstraint("authorization_revision > 0", name="ck_experiment_arm_revision"),
        CheckConstraint(
            "authorization_state IN ('ARMED', 'DISARMED')",
            name="ck_experiment_arm_state",
        ),
        CheckConstraint(
            "entry_authorized = (authorization_state = 'ARMED')",
            name="ck_experiment_arm_entry_authority",
        ),
        CheckConstraint(
            "existing_position_risk_management_preserved = true",
            name="ck_experiment_arm_management_preserved",
        ),
        CheckConstraint("runtime_state = 'NOT_CONNECTED'", name="ck_experiment_arm_runtime"),
        CheckConstraint("execution_eligible = false", name="ck_experiment_arm_execution"),
        CheckConstraint("paper_trading_only = true", name="ck_experiment_arm_paper"),
        CheckConstraint(
            "source_definition_hash NOT GLOB '*[^0-9a-f]*' AND length(source_definition_hash) = 64",
            name="ck_experiment_arm_source_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "protocol_hash NOT GLOB '*[^0-9a-f]*' AND length(protocol_hash) = 64",
            name="ck_experiment_arm_protocol_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "last_event_hash NOT GLOB '*[^0-9a-f]*' AND length(last_event_hash) = 64",
            name="ck_experiment_arm_event_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        ForeignKeyConstraint(
            ("experiment_id", "source_definition_hash", "protocol_hash"),
            (
                "compiled_experiment_versions.experiment_id",
                "compiled_experiment_versions.source_definition_hash",
                "compiled_experiment_versions.protocol_hash",
            ),
            name="fk_experiment_arm_state_compiled_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            (
                "experiment_id",
                "source_definition_hash",
                "protocol_hash",
                "authorization_revision",
                "authorization_state",
                "entry_authorized",
                "last_event_hash",
                "updated_at",
            ),
            (
                "experiment_arm_events.experiment_id",
                "experiment_arm_events.source_definition_hash",
                "experiment_arm_events.protocol_hash",
                "experiment_arm_events.authorization_revision",
                "experiment_arm_events.authorization_state",
                "experiment_arm_events.entry_authorized",
                "experiment_arm_events.event_hash",
                "experiment_arm_events.created_at",
            ),
            name="fk_experiment_arm_state_current_event",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_one_armed_experiment",
            "authorization_state",
            unique=True,
            sqlite_where=text("authorization_state = 'ARMED'"),
            postgresql_where=text("authorization_state = 'ARMED'"),
        ),
    )

    experiment_id: Mapped[UUID] = mapped_column(primary_key=True)
    source_definition_hash: Mapped[str] = mapped_column(String(64))
    protocol_hash: Mapped[str] = mapped_column(String(64))
    authorization_revision: Mapped[int] = mapped_column(Integer)
    authorization_state: Mapped[str] = mapped_column(String(16))
    entry_authorized: Mapped[bool] = mapped_column(Boolean)
    existing_position_risk_management_preserved: Mapped[bool] = mapped_column(Boolean)
    runtime_state: Mapped[str] = mapped_column(String(16))
    execution_eligible: Mapped[bool] = mapped_column(Boolean)
    paper_trading_only: Mapped[bool] = mapped_column(Boolean)
    last_event_hash: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExperimentArmEventRow(Base):
    __tablename__ = "experiment_arm_events"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "authorization_revision",
            name="uq_experiment_arm_event_revision",
        ),
        CheckConstraint("authorization_revision > 0", name="ck_experiment_arm_event_revision"),
        CheckConstraint("action IN ('ARM', 'DISARM')", name="ck_experiment_arm_event_action"),
        CheckConstraint(
            "authorization_state IN ('ARMED', 'DISARMED')",
            name="ck_experiment_arm_event_state",
        ),
        CheckConstraint(
            "(action = 'ARM' AND authorization_state = 'ARMED') OR "
            "(action = 'DISARM' AND authorization_state = 'DISARMED')",
            name="ck_experiment_arm_event_action_state",
        ),
        CheckConstraint(
            "entry_authorized = (authorization_state = 'ARMED')",
            name="ck_experiment_arm_event_entry_authority",
        ),
        CheckConstraint(
            "existing_position_risk_management_preserved = true",
            name="ck_experiment_arm_event_management_preserved",
        ),
        CheckConstraint("runtime_state = 'NOT_CONNECTED'", name="ck_experiment_arm_event_runtime"),
        CheckConstraint("execution_eligible = false", name="ck_experiment_arm_event_execution"),
        CheckConstraint("paper_trading_only = true", name="ck_experiment_arm_event_paper"),
        CheckConstraint(
            "source_definition_hash NOT GLOB '*[^0-9a-f]*' AND length(source_definition_hash) = 64",
            name="ck_experiment_arm_event_source_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "protocol_hash NOT GLOB '*[^0-9a-f]*' AND length(protocol_hash) = 64",
            name="ck_experiment_arm_event_protocol_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "event_hash NOT GLOB '*[^0-9a-f]*' AND length(event_hash) = 64",
            name="ck_experiment_arm_event_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        ForeignKeyConstraint(
            ("experiment_id", "source_definition_hash", "protocol_hash"),
            (
                "compiled_experiment_versions.experiment_id",
                "compiled_experiment_versions.source_definition_hash",
                "compiled_experiment_versions.protocol_hash",
            ),
            name="fk_experiment_arm_event_compiled_identity",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "experiment_id",
            "source_definition_hash",
            "protocol_hash",
            "authorization_revision",
            "authorization_state",
            "entry_authorized",
            "event_hash",
            "created_at",
            name="uq_experiment_arm_event_state_binding",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(index=True)
    source_definition_hash: Mapped[str] = mapped_column(String(64))
    protocol_hash: Mapped[str] = mapped_column(String(64))
    authorization_revision: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(16))
    authorization_state: Mapped[str] = mapped_column(String(16))
    entry_authorized: Mapped[bool] = mapped_column(Boolean)
    existing_position_risk_management_preserved: Mapped[bool] = mapped_column(Boolean)
    runtime_state: Mapped[str] = mapped_column(String(16))
    execution_eligible: Mapped[bool] = mapped_column(Boolean)
    paper_trading_only: Mapped[bool] = mapped_column(Boolean)
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceClassificationClaimRow(Base):
    __tablename__ = "evidence_classification_claims"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING', 'COMPLETED')",
            name="ck_evidence_classification_claim_state",
        ),
        CheckConstraint(
            "generation > 0",
            name="ck_evidence_classification_claim_generation",
        ),
        CheckConstraint(
            "(state = 'PENDING' AND lease_expires_at IS NOT NULL) OR "
            "(state = 'COMPLETED' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_evidence_classification_claim_ownership",
        ),
        CheckConstraint(
            "evidence_hash NOT GLOB '*[^0-9a-f]*' AND length(evidence_hash) = 64",
            name="ck_evidence_classification_claim_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "evidence_hash ~ '^[0-9a-f]{64}$'",
            name="ck_evidence_classification_claim_hash_postgresql",
        ).ddl_if(dialect="postgresql"),
    )

    evidence_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    generation: Mapped[int] = mapped_column(BigInteger)
    lease_owner: Mapped[UUID | None] = mapped_column(unique=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceClassificationRow(Base):
    __tablename__ = "evidence_classifications"
    __table_args__ = (
        CheckConstraint(
            "completed_generation > 0",
            name="ck_evidence_classification_generation",
        ),
        CheckConstraint(
            "classification_hash NOT GLOB '*[^0-9a-f]*' AND length(classification_hash) = 64",
            name="ck_evidence_classification_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "classification_hash ~ '^[0-9a-f]{64}$'",
            name="ck_evidence_classification_hash_postgresql",
        ).ddl_if(dialect="postgresql"),
    )

    evidence_hash: Mapped[str] = mapped_column(
        ForeignKey("evidence_classification_claims.evidence_hash"), primary_key=True
    )
    classifications_payload: Mapped[list[dict[str, object]]] = mapped_column(JSON_DOCUMENT)
    classification_hash: Mapped[str] = mapped_column(String(64), unique=True)
    completed_generation: Mapped[int] = mapped_column(BigInteger)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
