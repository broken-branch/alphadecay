ALTER TABLE account_roles
    ADD COLUMN IF NOT EXISTS execution_epoch bigint NOT NULL DEFAULT 0;
ALTER TABLE account_roles
    ADD COLUMN IF NOT EXISTS claim_generation bigint NOT NULL DEFAULT 0;
ALTER TABLE account_roles
    ADD COLUMN IF NOT EXISTS execution_lock_id uuid;
ALTER TABLE account_roles
    ADD COLUMN IF NOT EXISTS execution_lock_generation bigint NOT NULL DEFAULT 0;
ALTER TABLE account_roles
    ADD COLUMN IF NOT EXISTS recovery_pending boolean NOT NULL DEFAULT false;

CREATE UNIQUE INDEX IF NOT EXISTS uq_account_execution_lock_id
    ON account_roles (execution_lock_id)
    WHERE execution_lock_id IS NOT NULL;

ALTER TABLE execution_intents
    ADD COLUMN IF NOT EXISTS claim_token uuid;
ALTER TABLE execution_intents
    ADD COLUMN IF NOT EXISTS claim_generation bigint NOT NULL DEFAULT 0;
ALTER TABLE execution_intents
    ADD COLUMN IF NOT EXISTS execution_epoch bigint NOT NULL DEFAULT 0;
ALTER TABLE execution_intents
    ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;
ALTER TABLE execution_intents
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;

CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_intent_claim_token
    ON execution_intents (claim_token)
    WHERE claim_token IS NOT NULL;

DO $migration$
BEGIN
    ALTER TABLE account_roles DROP CONSTRAINT IF EXISTS ck_account_execution_lock;
    ALTER TABLE account_roles
        ADD CONSTRAINT ck_account_execution_lock CHECK (
            (
                execution_locked = false
                AND execution_lock_reason IS NULL
                AND execution_locked_at IS NULL
                AND execution_lock_id IS NULL
                AND recovery_pending = false
            )
            OR
            (
                execution_locked = true
                AND execution_lock_reason IN (
                    'ASSIGNMENT_SUSPECTED', 'RECONCILIATION_MISMATCH'
                )
                AND execution_locked_at IS NOT NULL
                AND execution_lock_id IS NOT NULL
                AND execution_lock_generation > 0
            )
        );
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_account_execution_fence'
          AND conrelid = 'account_roles'::regclass
    ) THEN
        ALTER TABLE account_roles
            ADD CONSTRAINT ck_account_execution_fence
            CHECK (execution_epoch >= 0 AND claim_generation >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_intent_claim_fence'
          AND conrelid = 'execution_intents'::regclass
    ) THEN
        ALTER TABLE execution_intents
            ADD CONSTRAINT ck_intent_claim_fence
            CHECK (
                claim_generation >= 0
                AND execution_epoch >= 0
                AND (
                    state <> 'CLAIMED'
                    OR (
                        claim_token IS NOT NULL
                        AND claim_generation > 0
                        AND heartbeat_at IS NOT NULL
                        AND lease_expires_at IS NOT NULL
                        AND claimed_at IS NOT NULL
                        AND heartbeat_at >= claimed_at
                        AND lease_expires_at > heartbeat_at
                    )
                )
            );
    END IF;
END
$migration$;

CREATE TABLE IF NOT EXISTS account_reconciliation_states (
    state_id uuid PRIMARY KEY,
    account_role varchar(16) NOT NULL REFERENCES account_roles(role),
    sequence bigint NOT NULL CHECK (sequence > 0),
    account_fingerprint varchar(64) NOT NULL,
    baseline_id uuid NOT NULL REFERENCES submission_baselines(baseline_id),
    baseline_captured_at timestamptz NOT NULL,
    accepted_at timestamptz NOT NULL,
    expected_cash numeric(18, 6) NOT NULL,
    expected_positions jsonb NOT NULL,
    expected_open_orders jsonb NOT NULL,
    known_activities jsonb NOT NULL,
    activity_complete_through timestamptz NOT NULL,
    state_hash varchar(64) NOT NULL UNIQUE,
    UNIQUE (account_role, sequence),
    CHECK (state_hash ~ '^[0-9a-f]{64}$'),
    CHECK (accepted_at >= baseline_captured_at),
    CHECK (activity_complete_through >= baseline_captured_at),
    CHECK (jsonb_typeof(expected_positions) = 'array'),
    CHECK (jsonb_typeof(expected_open_orders) = 'array'),
    CHECK (jsonb_typeof(known_activities) = 'array')
);

CREATE OR REPLACE FUNCTION validate_reconciliation_state_insert()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    account account_roles%ROWTYPE;
    baseline submission_baselines%ROWTYPE;
    prior_sequence bigint;
    funding jsonb;
BEGIN
    PERFORM pg_advisory_xact_lock(6748832221);
    SELECT * INTO account
    FROM account_roles
    WHERE role = NEW.account_role
    FOR UPDATE;
    SELECT * INTO baseline
    FROM submission_baselines
    WHERE baseline_id = NEW.baseline_id
    FOR UPDATE;
    SELECT COALESCE(max(sequence), 0) INTO prior_sequence
    FROM account_reconciliation_states
    WHERE account_role = NEW.account_role;

    IF account.role IS NULL
        OR baseline.baseline_id IS NULL
        OR account.role <> 'SUBMISSION'
        OR baseline.account_role <> account.role
        OR baseline.contaminated
        OR NEW.account_fingerprint <> account.account_fingerprint
        OR NEW.account_fingerprint <> baseline.account_fingerprint
        OR NEW.baseline_captured_at <> baseline.captured_at
        OR NEW.sequence <> prior_sequence + 1
    THEN
        RAISE EXCEPTION 'RECONCILIATION_STATE_NOT_CLEAN';
    END IF;
    IF NEW.sequence <> 1 THEN
        RAISE EXCEPTION 'RECONCILIATION_STATE_EVOLUTION_REQUIRES_PERMIT';
    END IF;

    funding := NEW.known_activities -> 0;
    IF NEW.expected_cash <> baseline.equity
        OR NEW.expected_positions <> '[]'::jsonb
        OR NEW.expected_open_orders <> '[]'::jsonb
        OR jsonb_array_length(NEW.known_activities) <> 1
        OR funding ->> 'activity_type' IS DISTINCT FROM 'INITIAL_FUNDING'
        OR funding ->> 'symbol' IS NOT NULL
        OR (funding ->> 'signed_quantity')::numeric <> baseline.equity
        OR (funding ->> 'occurred_at')::timestamptz > baseline.captured_at
        OR NEW.activity_complete_through <> baseline.captured_at
        OR NEW.accepted_at < baseline.captured_at
    THEN
        RAISE EXCEPTION 'RECONCILIATION_STATE_NOT_CLEAN';
    END IF;
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS account_reconciliation_state_insert_guard
    ON account_reconciliation_states;
CREATE TRIGGER account_reconciliation_state_insert_guard
BEFORE INSERT ON account_reconciliation_states
FOR EACH ROW EXECUTE FUNCTION validate_reconciliation_state_insert();

CREATE TABLE IF NOT EXISTS whole_account_reconciliations (
    reconciliation_id uuid PRIMARY KEY,
    reconciliation_hash varchar(64) NOT NULL UNIQUE,
    expectation_hash varchar(64) NOT NULL,
    execution_intent_id uuid NOT NULL REFERENCES execution_intents(intent_id),
    intent_digest varchar(64) NOT NULL,
    account_role varchar(16) NOT NULL REFERENCES account_roles(role),
    account_fingerprint varchar(64) NOT NULL,
    purpose varchar(16) NOT NULL
        CHECK (purpose IN ('SUBMIT', 'REPLACE', 'CANCEL', 'LOCK_CLEAR')),
    attempt_ordinal integer NOT NULL CHECK (attempt_ordinal BETWEEN 0 AND 3),
    request_hash varchar(64) NOT NULL,
    accepted_at timestamptz NOT NULL,
    expectation_payload jsonb NOT NULL,
    sweep_payload jsonb NOT NULL,
    positions_manifest_hash varchar(64) NOT NULL,
    orders_manifest_hash varchar(64) NOT NULL,
    activities_manifest_hash varchar(64) NOT NULL,
    safe boolean NOT NULL,
    block_codes jsonb NOT NULL,
    CHECK (reconciliation_hash ~ '^[0-9a-f]{64}$'),
    CHECK (expectation_hash ~ '^[0-9a-f]{64}$'),
    CHECK (intent_digest ~ '^[0-9a-f]{64}$'),
    CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    CHECK (positions_manifest_hash ~ '^[0-9a-f]{64}$'),
    CHECK (orders_manifest_hash ~ '^[0-9a-f]{64}$'),
    CHECK (activities_manifest_hash ~ '^[0-9a-f]{64}$'),
    CHECK (jsonb_typeof(expectation_payload) = 'object'),
    CHECK (jsonb_typeof(sweep_payload) = 'object'),
    CHECK (jsonb_typeof(block_codes) = 'array'),
    CHECK (safe = (jsonb_array_length(block_codes) = 0))
);

CREATE INDEX IF NOT EXISTS ix_whole_account_reconciliations_accepted
    ON whole_account_reconciliations (accepted_at, reconciliation_id);

CREATE TABLE IF NOT EXISTS broker_mutation_permits (
    permit_id uuid PRIMARY KEY,
    reconciliation_id uuid NOT NULL UNIQUE
        REFERENCES whole_account_reconciliations(reconciliation_id),
    execution_intent_id uuid NOT NULL REFERENCES execution_intents(intent_id),
    intent_digest varchar(64) NOT NULL,
    claim_token uuid NOT NULL,
    claim_generation bigint NOT NULL CHECK (claim_generation > 0),
    execution_epoch bigint NOT NULL CHECK (execution_epoch >= 0),
    mutation_kind varchar(16) NOT NULL
        CHECK (mutation_kind IN ('SUBMIT', 'REPLACE', 'CANCEL')),
    attempt_ordinal integer NOT NULL CHECK (attempt_ordinal BETWEEN 0 AND 3),
    permit_generation bigint NOT NULL CHECK (permit_generation > 0),
    predecessor_permit_id uuid UNIQUE REFERENCES broker_mutation_permits(permit_id),
    request_hash varchar(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    target_client_order_id varchar(64),
    target_provider_order_id varchar(128),
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > issued_at),
    state varchar(16) NOT NULL CHECK (state IN ('PREPARED', 'DISPATCHING', 'CONSUMED')),
    dispatch_nonce uuid UNIQUE,
    dispatch_acquired_at timestamptz,
    consumed_at timestamptz,
    outcome_hash varchar(64),
    UNIQUE (execution_intent_id, mutation_kind, attempt_ordinal, permit_generation),
    UNIQUE (execution_intent_id, mutation_kind, attempt_ordinal, request_hash),
    CHECK (
        (state = 'PREPARED' AND dispatch_nonce IS NULL
            AND dispatch_acquired_at IS NULL AND consumed_at IS NULL AND outcome_hash IS NULL)
        OR
        (state = 'DISPATCHING' AND dispatch_nonce IS NOT NULL
            AND dispatch_acquired_at IS NOT NULL AND consumed_at IS NULL AND outcome_hash IS NULL)
        OR
        (state = 'CONSUMED' AND dispatch_nonce IS NOT NULL
            AND dispatch_acquired_at IS NOT NULL AND consumed_at IS NOT NULL
            AND outcome_hash ~ '^[0-9a-f]{64}$')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_current_dispatchable_broker_permit
    ON broker_mutation_permits (execution_intent_id, mutation_kind, attempt_ordinal)
    WHERE state IN ('PREPARED', 'DISPATCHING');

ALTER TABLE order_attempts
    ADD COLUMN IF NOT EXISTS broker_permit_id uuid
        REFERENCES broker_mutation_permits(permit_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_order_attempt_broker_permit
    ON order_attempts (broker_permit_id)
    WHERE broker_permit_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS recovery_cases (
    case_id uuid PRIMARY KEY,
    account_role varchar(16) NOT NULL REFERENCES account_roles(role),
    execution_intent_id uuid REFERENCES execution_intents(intent_id),
    lock_id uuid NOT NULL,
    lock_generation bigint NOT NULL CHECK (lock_generation > 0),
    execution_epoch bigint NOT NULL CHECK (execution_epoch >= 0),
    claim_token uuid,
    owner_id_hash varchar(64) NOT NULL CHECK (owner_id_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key_hash varchar(64) NOT NULL UNIQUE
        CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    request_hash varchar(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    status varchar(16) NOT NULL CHECK (status IN ('REQUESTED', 'VERIFIED', 'CONSUMED')),
    requested_at timestamptz NOT NULL,
    verified_at timestamptz,
    accepted_evidence_hash varchar(64) UNIQUE,
    consumed_at timestamptz,
    CHECK (
        (status = 'REQUESTED' AND verified_at IS NULL
            AND accepted_evidence_hash IS NULL AND consumed_at IS NULL)
        OR
        (status = 'VERIFIED' AND verified_at IS NOT NULL
            AND accepted_evidence_hash ~ '^[0-9a-f]{64}$' AND consumed_at IS NULL)
        OR
        (status = 'CONSUMED' AND verified_at IS NOT NULL
            AND accepted_evidence_hash ~ '^[0-9a-f]{64}$' AND consumed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS recovery_events (
    event_id uuid PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES recovery_cases(case_id),
    event_sequence bigint NOT NULL CHECK (event_sequence > 0),
    event_type varchar(32) NOT NULL,
    evidence_hash varchar(64),
    event_payload jsonb NOT NULL CHECK (jsonb_typeof(event_payload) = 'object'),
    created_at timestamptz NOT NULL,
    UNIQUE (case_id, event_sequence),
    CHECK (evidence_hash IS NULL OR evidence_hash ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS recovery_certificates (
    certificate_id uuid PRIMARY KEY,
    case_id uuid NOT NULL UNIQUE REFERENCES recovery_cases(case_id),
    disposition varchar(48) NOT NULL,
    lock_id uuid NOT NULL,
    lock_generation bigint NOT NULL CHECK (lock_generation > 0),
    execution_epoch bigint NOT NULL CHECK (execution_epoch >= 0),
    evidence_hash varchar(64) NOT NULL UNIQUE CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL
);

CREATE OR REPLACE FUNCTION reject_whole_account_authority_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    RAISE EXCEPTION 'whole-account authority records are append-only';
END
$function$;

DROP TRIGGER IF EXISTS account_reconciliation_states_append_only
    ON account_reconciliation_states;
CREATE TRIGGER account_reconciliation_states_append_only
BEFORE UPDATE OR DELETE ON account_reconciliation_states
FOR EACH ROW EXECUTE FUNCTION reject_whole_account_authority_mutation();

DROP TRIGGER IF EXISTS whole_account_reconciliations_append_only
    ON whole_account_reconciliations;
CREATE TRIGGER whole_account_reconciliations_append_only
BEFORE UPDATE OR DELETE ON whole_account_reconciliations
FOR EACH ROW EXECUTE FUNCTION reject_whole_account_authority_mutation();

DROP TRIGGER IF EXISTS recovery_events_append_only ON recovery_events;
CREATE TRIGGER recovery_events_append_only
BEFORE UPDATE OR DELETE ON recovery_events
FOR EACH ROW EXECUTE FUNCTION reject_whole_account_authority_mutation();

DROP TRIGGER IF EXISTS recovery_certificates_append_only ON recovery_certificates;
CREATE TRIGGER recovery_certificates_append_only
BEFORE UPDATE OR DELETE ON recovery_certificates
FOR EACH ROW EXECUTE FUNCTION reject_whole_account_authority_mutation();

CREATE OR REPLACE FUNCTION guard_broker_permit_transition()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF (to_jsonb(NEW) - ARRAY[
            'state', 'dispatch_nonce', 'dispatch_acquired_at', 'consumed_at', 'outcome_hash'
        ]) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY[
            'state', 'dispatch_nonce', 'dispatch_acquired_at', 'consumed_at', 'outcome_hash'
        ])
    THEN
        RAISE EXCEPTION 'broker permit immutable fields changed';
    END IF;
    IF OLD.state = 'PREPARED' AND NEW.state = 'DISPATCHING'
        AND OLD.dispatch_nonce IS NULL
        AND NEW.dispatch_nonce IS NOT NULL
        AND NEW.dispatch_acquired_at IS NOT NULL
        AND NEW.consumed_at IS NULL
        AND NEW.outcome_hash IS NULL
    THEN
        RETURN NEW;
    END IF;
    IF OLD.state = 'DISPATCHING' AND NEW.state = 'CONSUMED'
        AND NEW.dispatch_nonce = OLD.dispatch_nonce
        AND NEW.dispatch_acquired_at = OLD.dispatch_acquired_at
        AND NEW.consumed_at IS NOT NULL
        AND NEW.outcome_hash ~ '^[0-9a-f]{64}$'
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'broker permit state transition invalid';
END
$function$;

DROP TRIGGER IF EXISTS broker_mutation_permit_update_guard
    ON broker_mutation_permits;
CREATE TRIGGER broker_mutation_permit_update_guard
BEFORE UPDATE ON broker_mutation_permits
FOR EACH ROW EXECUTE FUNCTION guard_broker_permit_transition();

DROP TRIGGER IF EXISTS broker_mutation_permit_delete_guard
    ON broker_mutation_permits;
CREATE TRIGGER broker_mutation_permit_delete_guard
BEFORE DELETE ON broker_mutation_permits
FOR EACH ROW EXECUTE FUNCTION reject_whole_account_authority_mutation();
