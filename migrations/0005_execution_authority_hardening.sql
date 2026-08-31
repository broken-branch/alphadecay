-- Additive authority changes. Migration 0004 is an immutable deployed anchor.

DO $migration$
DECLARE
    constraint_name text;
BEGIN
    SELECT constraint_record.conname
    INTO constraint_name
    FROM pg_constraint AS constraint_record
    WHERE constraint_record.conrelid = 'broker_mutation_permits'::regclass
      AND constraint_record.contype = 'u'
      AND (
          SELECT array_agg(attribute_record.attname::text ORDER BY key_column.ordinality)
          FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_column(attnum, ordinality)
          JOIN pg_attribute AS attribute_record
            ON attribute_record.attrelid = constraint_record.conrelid
           AND attribute_record.attnum = key_column.attnum
      ) = ARRAY[
          'execution_intent_id',
          'mutation_kind',
          'attempt_ordinal',
          'request_hash'
      ]::text[];

    IF constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE broker_mutation_permits DROP CONSTRAINT %I',
            constraint_name
        );
    END IF;
END
$migration$;

DO $migration$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT constraint_record.conname
        FROM pg_constraint AS constraint_record
        WHERE constraint_record.conrelid = 'broker_mutation_permits'::regclass
          AND constraint_record.contype = 'c'
          AND pg_get_constraintdef(constraint_record.oid) ILIKE '%state%'
    LOOP
        EXECUTE format(
            'ALTER TABLE broker_mutation_permits DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;
END
$migration$;

ALTER TABLE broker_mutation_permits
    ADD CONSTRAINT ck_broker_permit_state_v2 CHECK (
        state IN ('PREPARED', 'DISPATCHING', 'LOOKUP_ONLY', 'CONSUMED', 'EXPIRED')
    ),
    ADD CONSTRAINT ck_broker_permit_transition_fields_v2 CHECK (
        (state = 'PREPARED' AND dispatch_nonce IS NULL
            AND dispatch_acquired_at IS NULL AND consumed_at IS NULL AND outcome_hash IS NULL)
        OR
        (state = 'DISPATCHING' AND dispatch_nonce IS NOT NULL
            AND dispatch_acquired_at IS NOT NULL AND consumed_at IS NULL AND outcome_hash IS NULL)
        OR
        (state = 'LOOKUP_ONLY' AND dispatch_nonce IS NOT NULL
            AND dispatch_acquired_at IS NOT NULL AND consumed_at IS NULL AND outcome_hash IS NULL)
        OR
        (state = 'CONSUMED' AND dispatch_nonce IS NOT NULL
            AND dispatch_acquired_at IS NOT NULL AND consumed_at IS NOT NULL
            AND outcome_hash ~ '^[0-9a-f]{64}$')
        OR
        (state = 'EXPIRED' AND dispatch_nonce IS NULL
            AND dispatch_acquired_at IS NULL AND consumed_at IS NOT NULL AND outcome_hash IS NULL)
    );

DROP INDEX IF EXISTS uq_current_dispatchable_broker_permit;
CREATE UNIQUE INDEX uq_current_dispatchable_broker_permit
    ON broker_mutation_permits (execution_intent_id, mutation_kind, attempt_ordinal)
    WHERE state IN ('PREPARED', 'DISPATCHING', 'LOOKUP_ONLY');

CREATE TABLE attempt_observations (
    observation_id uuid PRIMARY KEY,
    permit_id uuid NOT NULL REFERENCES broker_mutation_permits(permit_id),
    execution_intent_id uuid NOT NULL REFERENCES execution_intents(intent_id),
    attempt_id uuid NOT NULL REFERENCES order_attempts(attempt_id),
    attempt_ordinal integer NOT NULL CHECK (attempt_ordinal BETWEEN 0 AND 3),
    observation_sequence bigint NOT NULL CHECK (observation_sequence > 0),
    source varchar(32) NOT NULL CHECK (
        source IN ('DISPATCH_OUTCOME', 'TARGETED_LOOKUP')
    ),
    provider_present boolean NOT NULL,
    observed_payload jsonb,
    observed_at timestamptz NOT NULL,
    observation_hash varchar(64) NOT NULL UNIQUE
        CHECK (observation_hash ~ '^[0-9a-f]{64}$'),
    UNIQUE (execution_intent_id, observation_sequence),
    CHECK (
        (provider_present = true AND jsonb_typeof(observed_payload) = 'object')
        OR (provider_present = false AND observed_payload IS NULL)
    )
);

ALTER TABLE execution_certificates
    ADD COLUMN reconciliation_id uuid
        REFERENCES whole_account_reconciliations(reconciliation_id),
    ADD COLUMN reconciliation_hash varchar(64),
    ADD COLUMN last_observation_hash varchar(64)
        REFERENCES attempt_observations(observation_hash),
    ADD CONSTRAINT ck_execution_certificate_reconciliation_provenance CHECK (
        (reconciliation_id IS NULL AND reconciliation_hash IS NULL
            AND last_observation_hash IS NULL)
        OR
        (reconciliation_id IS NOT NULL
            AND reconciliation_hash ~ '^[0-9a-f]{64}$'
            AND last_observation_hash ~ '^[0-9a-f]{64}$')
    );

CREATE UNIQUE INDEX uq_execution_certificate_reconciliation
    ON execution_certificates (reconciliation_id)
    WHERE reconciliation_id IS NOT NULL;
CREATE UNIQUE INDEX uq_execution_certificate_reconciliation_hash
    ON execution_certificates (reconciliation_hash)
    WHERE reconciliation_hash IS NOT NULL;
CREATE UNIQUE INDEX uq_execution_certificate_last_observation
    ON execution_certificates (last_observation_hash)
    WHERE last_observation_hash IS NOT NULL;

DROP TRIGGER IF EXISTS attempt_observations_append_only ON attempt_observations;
CREATE TRIGGER attempt_observations_append_only
BEFORE UPDATE OR DELETE ON attempt_observations
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
    IF OLD.state = 'DISPATCHING' AND NEW.state = 'LOOKUP_ONLY'
        AND NEW.dispatch_nonce = OLD.dispatch_nonce
        AND NEW.dispatch_acquired_at = OLD.dispatch_acquired_at
        AND NEW.consumed_at IS NULL
        AND NEW.outcome_hash IS NULL
    THEN
        RETURN NEW;
    END IF;
    IF OLD.state = 'LOOKUP_ONLY' AND NEW.state = 'CONSUMED'
        AND NEW.dispatch_nonce = OLD.dispatch_nonce
        AND NEW.dispatch_acquired_at = OLD.dispatch_acquired_at
        AND NEW.consumed_at IS NOT NULL
        AND NEW.outcome_hash ~ '^[0-9a-f]{64}$'
    THEN
        RETURN NEW;
    END IF;
    IF OLD.state = 'PREPARED' AND NEW.state = 'EXPIRED'
        AND OLD.dispatch_nonce IS NULL
        AND NEW.dispatch_nonce IS NULL
        AND NEW.dispatch_acquired_at IS NULL
        AND NEW.consumed_at IS NOT NULL
        AND NEW.outcome_hash IS NULL
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'broker permit state transition invalid';
END
$function$;
