ALTER TABLE account_reconciliation_states
    ADD COLUMN resolved_activity_hashes jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD CONSTRAINT ck_reconciliation_state_resolved_activity_hashes CHECK (
        jsonb_typeof(resolved_activity_hashes) = 'array'
    );

ALTER TABLE account_roles
    DROP CONSTRAINT ck_account_execution_lock;

ALTER TABLE account_roles
    ADD CONSTRAINT ck_account_execution_lock CHECK (
        (
            execution_locked = false
            AND execution_lock_reason IS NULL
            AND execution_locked_at IS NULL
            AND execution_lock_id IS NULL
            AND recovery_pending = false
        ) OR (
            execution_locked = true
            AND execution_lock_reason IN (
                'ASSIGNMENT_SUSPECTED',
                'RECONCILIATION_MISMATCH',
                'ENTRY_EQUITY_FLOOR',
                'ENTRY_OPEN_POSITION_LIMIT',
                'ENTRY_LIMITS_REQUIRED',
                'ENTRY_POLICY_AUTHORITY_MISMATCH',
                'ENTRY_COUNT_EXHAUSTED',
                'ENTRY_POSITION_RISK_EXHAUSTED',
                'ENTRY_RISK_EXHAUSTED',
                'ENTRY_QUANTITY_EXHAUSTED'
            )
            AND execution_locked_at IS NOT NULL
            AND execution_lock_id IS NOT NULL
            AND execution_lock_generation > 0
        )
    );

CREATE OR REPLACE FUNCTION validate_reconciliation_state_insert()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    account account_roles%ROWTYPE;
    baseline submission_baselines%ROWTYPE;
    prior_state account_reconciliation_states%ROWTYPE;
    authority whole_account_reconciliations%ROWTYPE;
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

    IF NEW.sequence = 1 THEN
        funding := NEW.known_activities -> 0;
        IF NEW.expected_cash <> baseline.equity
            OR NEW.expected_positions <> '[]'::jsonb
            OR NEW.expected_open_orders <> '[]'::jsonb
            OR NEW.resolved_activity_hashes <> '[]'::jsonb
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
    END IF;

    SELECT * INTO prior_state
    FROM account_reconciliation_states
    WHERE account_role = NEW.account_role
      AND sequence = NEW.sequence - 1
    FOR UPDATE;

    SELECT reconciliation.* INTO authority
    FROM whole_account_reconciliations AS reconciliation
    WHERE reconciliation.account_role = NEW.account_role
      AND reconciliation.account_fingerprint = NEW.account_fingerprint
      AND reconciliation.accepted_at = NEW.accepted_at
      AND reconciliation.safe
      AND (reconciliation.expectation_payload ->> 'expected_cash')::numeric
            = NEW.expected_cash
      AND reconciliation.expectation_payload -> 'expected_positions'
            = NEW.expected_positions
      AND reconciliation.expectation_payload -> 'expected_open_orders'
            = NEW.expected_open_orders
      AND reconciliation.sweep_payload -> 'activities' = NEW.known_activities
      AND (
            reconciliation.sweep_payload -> 'activity_pagination'
                ->> 'visibility_complete_through'
          )::timestamptz = NEW.activity_complete_through
      AND EXISTS (
          SELECT 1
          FROM attempt_observations AS observation
          JOIN broker_mutation_permits AS permit
            ON permit.permit_id = observation.permit_id
          WHERE observation.execution_intent_id = reconciliation.execution_intent_id
            AND observation.attempt_ordinal = reconciliation.attempt_ordinal
            AND observation.provider_present
            AND observation.observed_at <= reconciliation.accepted_at
            AND permit.state = 'CONSUMED'
      )
    ORDER BY reconciliation.reconciliation_id
    LIMIT 1;

    IF prior_state.state_id IS NULL
        OR authority.reconciliation_id IS NULL
        OR NEW.accepted_at < prior_state.accepted_at
        OR NEW.activity_complete_through < prior_state.activity_complete_through
        OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(NEW.resolved_activity_hashes) AS resolved(hash)
            WHERE NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.known_activities) AS activity(value)
                WHERE activity.value ->> 'activity_id_hash' = resolved.hash
            )
        )
    THEN
        RAISE EXCEPTION 'RECONCILIATION_STATE_EVOLUTION_UNAUTHORIZED';
    END IF;
    RETURN NEW;
END
$function$;
