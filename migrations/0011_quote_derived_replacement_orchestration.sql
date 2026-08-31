ALTER TABLE order_attempts
    ADD COLUMN limit_price numeric(18, 6),
    ADD COLUMN quote_hash varchar(64),
    ADD COLUMN quote_source_timestamps jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN quote_retrieved_at timestamptz,
    ADD COLUMN timing_authority_at timestamptz,
    ADD COLUMN prior_request_hash varchar(64);

ALTER TABLE order_attempts
    ADD CONSTRAINT ck_order_attempt_quote_authority CHECK (
        (quote_hash IS NULL
            AND jsonb_array_length(quote_source_timestamps) = 0
            AND quote_retrieved_at IS NULL
            AND timing_authority_at IS NULL
            AND prior_request_hash IS NULL)
        OR
        (attempt_ordinal > 0
            AND quote_hash ~ '^[0-9a-f]{64}$'
            AND jsonb_array_length(quote_source_timestamps) > 0
            AND quote_retrieved_at IS NOT NULL
            AND timing_authority_at IS NOT NULL
            AND prior_request_hash ~ '^[0-9a-f]{64}$')
    );

ALTER TABLE broker_mutation_permits
    ADD COLUMN limit_price numeric(18, 6),
    ADD COLUMN quote_hash varchar(64),
    ADD COLUMN quote_source_timestamps jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN quote_retrieved_at timestamptz,
    ADD COLUMN timing_authority_at timestamptz,
    ADD COLUMN prior_request_hash varchar(64);

DROP TRIGGER broker_mutation_permit_update_guard ON broker_mutation_permits;

UPDATE broker_mutation_permits AS permit
SET limit_price = COALESCE(attempt.limit_price, intent.minimum_limit),
    quote_hash = attempt.quote_hash,
    quote_source_timestamps = attempt.quote_source_timestamps,
    quote_retrieved_at = attempt.quote_retrieved_at,
    timing_authority_at = attempt.timing_authority_at,
    prior_request_hash = attempt.prior_request_hash
FROM order_attempts AS attempt,
     execution_intents AS intent
WHERE attempt.execution_intent_id = permit.execution_intent_id
  AND attempt.attempt_ordinal = permit.attempt_ordinal
  AND intent.intent_id = permit.execution_intent_id;

CREATE TRIGGER broker_mutation_permit_update_guard
BEFORE UPDATE ON broker_mutation_permits
FOR EACH ROW EXECUTE FUNCTION guard_broker_permit_transition();

ALTER TABLE broker_mutation_permits
    ALTER COLUMN limit_price SET NOT NULL,
    ADD CONSTRAINT ck_broker_permit_quote_authority CHECK (
        (quote_hash IS NULL
            AND jsonb_array_length(quote_source_timestamps) = 0
            AND quote_retrieved_at IS NULL
            AND timing_authority_at IS NULL
            AND prior_request_hash IS NULL)
        OR (
            quote_hash ~ '^[0-9a-f]{64}$'
            AND jsonb_array_length(quote_source_timestamps) > 0
            AND quote_retrieved_at IS NOT NULL
            AND timing_authority_at IS NOT NULL
            AND prior_request_hash ~ '^[0-9a-f]{64}$'
        )
    );
