ALTER TABLE account_reconciliation_states
    ADD COLUMN predecessor_state_id uuid
        REFERENCES account_reconciliation_states(state_id),
    ADD COLUMN authority_reconciliation_id uuid
        REFERENCES whole_account_reconciliations(reconciliation_id),
    ADD COLUMN transition_hash text;

UPDATE account_reconciliation_states AS state
SET predecessor_state_id = predecessor.state_id
FROM account_reconciliation_states AS predecessor
WHERE state.sequence > 1
  AND predecessor.account_role = state.account_role
  AND predecessor.sequence = state.sequence - 1;

UPDATE account_reconciliation_states AS state
SET authority_reconciliation_id = (
    SELECT reconciliation.reconciliation_id
    FROM whole_account_reconciliations AS reconciliation
    WHERE reconciliation.account_role = state.account_role
      AND reconciliation.account_fingerprint = state.account_fingerprint
      AND reconciliation.accepted_at = state.accepted_at
      AND reconciliation.safe
      AND (reconciliation.expectation_payload ->> 'expected_cash')::numeric
            = state.expected_cash
      AND reconciliation.expectation_payload -> 'expected_positions'
            = state.expected_positions
      AND reconciliation.expectation_payload -> 'expected_open_orders'
            = state.expected_open_orders
      AND reconciliation.expectation_payload -> 'resolved_activity_hashes'
            = state.resolved_activity_hashes
      AND reconciliation.sweep_payload -> 'activities' = state.known_activities
    ORDER BY reconciliation.reconciliation_id
    LIMIT 1
)
WHERE state.sequence > 1;

DO $block$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM account_reconciliation_states
        WHERE sequence > 1
          AND (predecessor_state_id IS NULL OR authority_reconciliation_id IS NULL)
    ) OR EXISTS (
        SELECT authority_reconciliation_id
        FROM account_reconciliation_states
        WHERE authority_reconciliation_id IS NOT NULL
        GROUP BY authority_reconciliation_id
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'RECONCILIATION_TRANSITION_BACKFILL_AMBIGUOUS';
    END IF;
END
$block$;

UPDATE account_reconciliation_states AS state
SET transition_hash = encode(
    sha256(
        convert_to(
            concat_ws(
                '|',
                'alphadecay.reconciliation-transition.v1',
                predecessor.state_hash,
                authority.reconciliation_hash,
                state.state_hash,
                state.expected_cash::text,
                state.expected_positions::text,
                state.expected_open_orders::text,
                state.known_activities::text,
                state.resolved_activity_hashes::text,
                state.activity_complete_through::text
            ),
            'UTF8'
        )
    ),
    'hex'
)
FROM account_reconciliation_states AS predecessor,
     whole_account_reconciliations AS authority
WHERE state.sequence > 1
  AND predecessor.state_id = state.predecessor_state_id
  AND authority.reconciliation_id = state.authority_reconciliation_id;

ALTER TABLE account_reconciliation_states
    ADD CONSTRAINT uq_reconciliation_state_predecessor UNIQUE (predecessor_state_id),
    ADD CONSTRAINT uq_reconciliation_state_authority UNIQUE (authority_reconciliation_id),
    ADD CONSTRAINT uq_reconciliation_state_transition_hash UNIQUE (transition_hash),
    ADD CONSTRAINT ck_reconciliation_state_transition_authority CHECK (
        (sequence = 1
            AND predecessor_state_id IS NULL
            AND authority_reconciliation_id IS NULL
            AND transition_hash IS NULL)
        OR
        (sequence > 1
            AND predecessor_state_id IS NOT NULL
            AND authority_reconciliation_id IS NOT NULL
            AND length(transition_hash) = 64)
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
    expected_transition_hash text;
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
            OR NEW.predecessor_state_id IS NOT NULL
            OR NEW.authority_reconciliation_id IS NOT NULL
            OR NEW.transition_hash IS NOT NULL
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
    WHERE state_id = NEW.predecessor_state_id
      AND account_role = NEW.account_role
      AND sequence = NEW.sequence - 1
    FOR UPDATE;

    SELECT * INTO authority
    FROM whole_account_reconciliations
    WHERE reconciliation_id = NEW.authority_reconciliation_id
    FOR UPDATE;

    expected_transition_hash := encode(
        sha256(
            convert_to(
                concat_ws(
                    '|',
                    'alphadecay.reconciliation-transition.v1',
                    prior_state.state_hash,
                    authority.reconciliation_hash,
                    NEW.state_hash,
                    NEW.expected_cash::text,
                    NEW.expected_positions::text,
                    NEW.expected_open_orders::text,
                    NEW.known_activities::text,
                    NEW.resolved_activity_hashes::text,
                    NEW.activity_complete_through::text
                ),
                'UTF8'
            )
        ),
        'hex'
    );

    IF prior_state.state_id IS NULL
        OR authority.reconciliation_id IS NULL
        OR authority.account_role <> NEW.account_role
        OR authority.account_fingerprint <> NEW.account_fingerprint
        OR authority.accepted_at <> NEW.accepted_at
        OR NOT authority.safe
        OR (authority.expectation_payload ->> 'expected_cash')::numeric
            <> NEW.expected_cash
        OR authority.expectation_payload -> 'expected_positions'
            <> NEW.expected_positions
        OR authority.expectation_payload -> 'expected_open_orders'
            <> NEW.expected_open_orders
        OR authority.expectation_payload -> 'resolved_activity_hashes'
            <> NEW.resolved_activity_hashes
        OR authority.expectation_payload -> 'known_activities'
            <> NEW.known_activities
        OR authority.sweep_payload -> 'activities' <> NEW.known_activities
        OR (
            authority.expectation_payload ->> 'required_activity_complete_through'
        )::timestamptz <> prior_state.activity_complete_through
        OR (
            authority.sweep_payload -> 'activity_pagination'
                ->> 'visibility_complete_through'
        )::timestamptz <> NEW.activity_complete_through
        OR NEW.accepted_at < prior_state.accepted_at
        OR NEW.activity_complete_through < prior_state.activity_complete_through
        OR (NEW.transition_hash IS NOT NULL
            AND NEW.transition_hash <> expected_transition_hash)
        OR NOT EXISTS (
            SELECT 1
            FROM attempt_observations AS observation
            JOIN broker_mutation_permits AS permit
              ON permit.permit_id = observation.permit_id
            WHERE observation.execution_intent_id = authority.execution_intent_id
              AND observation.attempt_ordinal = authority.attempt_ordinal
              AND observation.provider_present
              AND observation.observed_at <= authority.accepted_at
              AND permit.state = 'CONSUMED'
        )
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
    NEW.transition_hash := expected_transition_hash;
    RETURN NEW;
END
$function$;
