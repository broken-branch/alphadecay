ALTER TABLE account_reconciliation_states
    ADD COLUMN authority_permit_id uuid REFERENCES broker_mutation_permits(permit_id),
    ADD COLUMN authority_observation_id uuid REFERENCES attempt_observations(observation_id),
    ADD COLUMN authority_permit_request_hash text;

UPDATE account_reconciliation_states AS state
SET authority_observation_id = (
    SELECT observation.observation_id
    FROM whole_account_reconciliations AS authority
    JOIN attempt_observations AS observation
      ON observation.execution_intent_id = authority.execution_intent_id
    WHERE authority.reconciliation_id = state.authority_reconciliation_id
      AND observation.provider_present
    ORDER BY observation.observation_sequence DESC, observation.observation_id DESC
    LIMIT 1
)
WHERE state.sequence > 1;

UPDATE account_reconciliation_states AS state
SET authority_permit_id = observation.permit_id,
    authority_permit_request_hash = permit.request_hash
FROM attempt_observations AS observation,
     broker_mutation_permits AS permit
WHERE state.sequence > 1
  AND observation.observation_id = state.authority_observation_id
  AND permit.permit_id = observation.permit_id;

DO $block$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM account_reconciliation_states AS state
        LEFT JOIN whole_account_reconciliations AS authority
          ON authority.reconciliation_id = state.authority_reconciliation_id
        LEFT JOIN attempt_observations AS observation
          ON observation.observation_id = state.authority_observation_id
        LEFT JOIN broker_mutation_permits AS permit
          ON permit.permit_id = state.authority_permit_id
        LEFT JOIN order_attempts AS attempt
          ON attempt.attempt_id = observation.attempt_id
        LEFT JOIN order_attempts AS predecessor
          ON predecessor.attempt_id = attempt.replaces_attempt_id
        WHERE state.sequence > 1
          AND (
              authority.reconciliation_id IS NULL
              OR observation.observation_id IS NULL
              OR permit.permit_id IS NULL
              OR attempt.attempt_id IS NULL
              OR observation.permit_id IS DISTINCT FROM permit.permit_id
              OR observation.attempt_id IS DISTINCT FROM attempt.attempt_id
              OR observation.execution_intent_id
                  IS DISTINCT FROM authority.execution_intent_id
              OR observation.execution_intent_id
                  IS DISTINCT FROM attempt.execution_intent_id
              OR observation.attempt_ordinal IS DISTINCT FROM authority.attempt_ordinal
              OR observation.attempt_ordinal IS DISTINCT FROM attempt.attempt_ordinal
              OR observation.provider_present IS DISTINCT FROM true
              OR NOT (observation.observed_payload ?& ARRAY[
                  'intent_id', 'ordinal', 'client_order_id', 'request_hash', 'state',
                  'replaces_client_order_id', 'provider_order_id', 'filled_quantity',
                  'quantity', 'fill_cash_flow'
              ])
              OR observation.observed_payload ->> 'intent_id'
                  IS DISTINCT FROM attempt.execution_intent_id::text
              OR (observation.observed_payload ->> 'ordinal')::integer
                  IS DISTINCT FROM attempt.attempt_ordinal
              OR observation.observed_payload ->> 'client_order_id'
                  IS DISTINCT FROM attempt.client_order_id
              OR observation.observed_payload ->> 'request_hash'
                  IS DISTINCT FROM attempt.request_hash
              OR observation.observed_payload ->> 'state' IS DISTINCT FROM attempt.state
              OR observation.observed_payload ->> 'replaces_client_order_id'
                  IS DISTINCT FROM predecessor.client_order_id
              OR observation.observed_payload ->> 'provider_order_id'
                  IS DISTINCT FROM attempt.provider_order_id
              OR (observation.observed_payload ->> 'filled_quantity')::integer
                  IS DISTINCT FROM attempt.filled_quantity
              OR (observation.observed_payload ->> 'quantity')::integer
                  IS DISTINCT FROM attempt.quantity
              OR (observation.observed_payload ->> 'fill_cash_flow')::numeric
                  IS DISTINCT FROM attempt.filled_cash_flow
              OR observation.observed_payload ->> 'state'
                  NOT IN ('FILLED', 'REJECTED', 'CANCELED', 'EXPIRED')
              OR permit.state IS DISTINCT FROM 'CONSUMED'
              OR permit.execution_intent_id
                  IS DISTINCT FROM authority.execution_intent_id
              OR permit.intent_digest IS DISTINCT FROM authority.intent_digest
              OR permit.attempt_ordinal IS DISTINCT FROM authority.attempt_ordinal
              OR permit.mutation_kind IS DISTINCT FROM authority.purpose
              OR permit.request_hash IS DISTINCT FROM state.authority_permit_request_hash
              OR authority.expectation_payload ->> 'purpose'
                  IS DISTINCT FROM authority.purpose
              OR authority.expectation_payload ->> 'intent_id'
                  IS DISTINCT FROM authority.execution_intent_id::text
              OR authority.expectation_payload ->> 'intent_digest'
                  IS DISTINCT FROM authority.intent_digest
              OR (authority.expectation_payload ->> 'attempt_ordinal')::integer
                  IS DISTINCT FROM authority.attempt_ordinal
              OR authority.expectation_payload ->> 'request_hash'
                  IS DISTINCT FROM authority.request_hash
              OR (permit.mutation_kind = 'SUBMIT' AND (
                  permit.target_client_order_id IS NOT NULL
                  OR permit.target_provider_order_id IS NOT NULL
              ))
              OR (permit.mutation_kind = 'REPLACE' AND (
                  predecessor.attempt_id IS NULL
                  OR permit.target_client_order_id IS DISTINCT FROM predecessor.client_order_id
                  OR permit.target_provider_order_id IS DISTINCT FROM predecessor.provider_order_id
              ))
              OR (permit.mutation_kind = 'CANCEL' AND (
                  permit.target_client_order_id IS DISTINCT FROM attempt.client_order_id
                  OR permit.target_provider_order_id IS DISTINCT FROM attempt.provider_order_id
              ))
              OR observation.observed_at > (
                  authority.sweep_payload ->> 'retrieval_started_at'
              )::timestamptz
              OR (
                  authority.sweep_payload ->> 'retrieval_started_at'
              )::timestamptz > authority.accepted_at
              OR (
                  authority.sweep_payload ->> 'retrieval_completed_at'
              )::timestamptz > authority.accepted_at
              OR EXISTS (
                  SELECT 1
                  FROM attempt_observations AS later
                  WHERE later.execution_intent_id = observation.execution_intent_id
                    AND later.observation_sequence > observation.observation_sequence
              )
          )
    ) THEN
        RAISE EXCEPTION 'RECONCILIATION_OBSERVATION_BACKFILL_AMBIGUOUS';
    END IF;
END
$block$;

ALTER TABLE account_reconciliation_states
    ADD CONSTRAINT uq_reconciliation_state_authority_permit UNIQUE (authority_permit_id),
    ADD CONSTRAINT uq_reconciliation_state_authority_observation
        UNIQUE (authority_observation_id),
    ADD CONSTRAINT ck_reconciliation_state_observation_authority CHECK (
        (sequence = 1
            AND authority_permit_id IS NULL
            AND authority_observation_id IS NULL
            AND authority_permit_request_hash IS NULL)
        OR
        (sequence > 1
            AND authority_permit_id IS NOT NULL
            AND authority_observation_id IS NOT NULL
            AND authority_permit_request_hash IS NOT NULL
            AND length(authority_permit_request_hash) = 64)
    );

CREATE FUNCTION validate_reconciliation_observation_authority_insert()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    authority whole_account_reconciliations%ROWTYPE;
    permit broker_mutation_permits%ROWTYPE;
    observation attempt_observations%ROWTYPE;
    attempt order_attempts%ROWTYPE;
    predecessor order_attempts%ROWTYPE;
BEGIN
    IF NEW.sequence = 1 THEN
        IF NEW.authority_permit_id IS NOT NULL
            OR NEW.authority_observation_id IS NOT NULL
            OR NEW.authority_permit_request_hash IS NOT NULL
        THEN
            RAISE EXCEPTION 'RECONCILIATION_OBSERVATION_AUTHORITY_INVALID';
        END IF;
        RETURN NEW;
    END IF;

    SELECT * INTO authority
    FROM whole_account_reconciliations
    WHERE reconciliation_id = NEW.authority_reconciliation_id
    FOR UPDATE;
    SELECT * INTO permit
    FROM broker_mutation_permits
    WHERE permit_id = NEW.authority_permit_id
    FOR UPDATE;
    SELECT * INTO observation
    FROM attempt_observations
    WHERE observation_id = NEW.authority_observation_id
    FOR UPDATE;
    SELECT * INTO attempt
    FROM order_attempts
    WHERE attempt_id = observation.attempt_id
    FOR UPDATE;
    IF attempt.replaces_attempt_id IS NOT NULL THEN
        SELECT * INTO predecessor
        FROM order_attempts
        WHERE attempt_id = attempt.replaces_attempt_id
        FOR UPDATE;
    END IF;

    IF authority.reconciliation_id IS NULL
        OR permit.permit_id IS NULL
        OR observation.observation_id IS NULL
        OR attempt.attempt_id IS NULL
        OR observation.permit_id IS DISTINCT FROM permit.permit_id
        OR observation.attempt_id IS DISTINCT FROM attempt.attempt_id
        OR observation.execution_intent_id
            IS DISTINCT FROM authority.execution_intent_id
        OR observation.execution_intent_id
            IS DISTINCT FROM attempt.execution_intent_id
        OR observation.attempt_ordinal IS DISTINCT FROM authority.attempt_ordinal
        OR observation.attempt_ordinal IS DISTINCT FROM attempt.attempt_ordinal
        OR observation.provider_present IS DISTINCT FROM true
        OR NOT (observation.observed_payload ?& ARRAY[
            'intent_id', 'ordinal', 'client_order_id', 'request_hash', 'state',
            'replaces_client_order_id', 'provider_order_id', 'filled_quantity',
            'quantity', 'fill_cash_flow'
        ])
        OR observation.observed_payload ->> 'intent_id'
            IS DISTINCT FROM attempt.execution_intent_id::text
        OR (observation.observed_payload ->> 'ordinal')::integer
            IS DISTINCT FROM attempt.attempt_ordinal
        OR observation.observed_payload ->> 'client_order_id'
            IS DISTINCT FROM attempt.client_order_id
        OR observation.observed_payload ->> 'request_hash'
            IS DISTINCT FROM attempt.request_hash
        OR observation.observed_payload ->> 'state' IS DISTINCT FROM attempt.state
        OR observation.observed_payload ->> 'replaces_client_order_id'
            IS DISTINCT FROM predecessor.client_order_id
        OR observation.observed_payload ->> 'provider_order_id'
            IS DISTINCT FROM attempt.provider_order_id
        OR (observation.observed_payload ->> 'filled_quantity')::integer
            IS DISTINCT FROM attempt.filled_quantity
        OR (observation.observed_payload ->> 'quantity')::integer
            IS DISTINCT FROM attempt.quantity
        OR (observation.observed_payload ->> 'fill_cash_flow')::numeric
            IS DISTINCT FROM attempt.filled_cash_flow
        OR observation.observed_payload ->> 'state'
            NOT IN ('FILLED', 'REJECTED', 'CANCELED', 'EXPIRED')
        OR permit.state IS DISTINCT FROM 'CONSUMED'
        OR permit.execution_intent_id
            IS DISTINCT FROM authority.execution_intent_id
        OR permit.intent_digest IS DISTINCT FROM authority.intent_digest
        OR permit.attempt_ordinal IS DISTINCT FROM authority.attempt_ordinal
        OR permit.mutation_kind IS DISTINCT FROM authority.purpose
        OR permit.request_hash IS DISTINCT FROM NEW.authority_permit_request_hash
        OR authority.expectation_payload ->> 'purpose'
            IS DISTINCT FROM authority.purpose
        OR authority.expectation_payload ->> 'intent_id'
            IS DISTINCT FROM authority.execution_intent_id::text
        OR authority.expectation_payload ->> 'intent_digest'
            IS DISTINCT FROM authority.intent_digest
        OR (authority.expectation_payload ->> 'attempt_ordinal')::integer
            IS DISTINCT FROM authority.attempt_ordinal
        OR authority.expectation_payload ->> 'request_hash'
            IS DISTINCT FROM authority.request_hash
        OR (permit.mutation_kind = 'SUBMIT' AND (
            permit.target_client_order_id IS NOT NULL
            OR permit.target_provider_order_id IS NOT NULL
        ))
        OR (permit.mutation_kind = 'REPLACE' AND (
            predecessor.attempt_id IS NULL
            OR permit.target_client_order_id IS DISTINCT FROM predecessor.client_order_id
            OR permit.target_provider_order_id IS DISTINCT FROM predecessor.provider_order_id
        ))
        OR (permit.mutation_kind = 'CANCEL' AND (
            permit.target_client_order_id IS DISTINCT FROM attempt.client_order_id
            OR permit.target_provider_order_id IS DISTINCT FROM attempt.provider_order_id
        ))
        OR observation.observed_at > (
            authority.sweep_payload ->> 'retrieval_started_at'
        )::timestamptz
        OR (
            authority.sweep_payload ->> 'retrieval_started_at'
        )::timestamptz > authority.accepted_at
        OR (
            authority.sweep_payload ->> 'retrieval_completed_at'
        )::timestamptz > authority.accepted_at
        OR EXISTS (
            SELECT 1
            FROM attempt_observations AS later
            WHERE later.execution_intent_id = observation.execution_intent_id
              AND later.observation_sequence > observation.observation_sequence
        )
    THEN
        RAISE EXCEPTION 'RECONCILIATION_OBSERVATION_AUTHORITY_INVALID';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER reconciliation_observation_authority_insert_guard
BEFORE INSERT ON account_reconciliation_states
FOR EACH ROW EXECUTE FUNCTION validate_reconciliation_observation_authority_insert();
