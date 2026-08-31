CREATE TABLE model_call_budgets (
    model varchar(40) PRIMARY KEY,
    request_count integer NOT NULL DEFAULT 0,
    hard_limit integer NOT NULL DEFAULT 50,
    CONSTRAINT ck_model_call_budget_identity
        CHECK (model = 'gemini-3.7-flash' AND hard_limit = 50),
    CONSTRAINT ck_model_call_budget_count
        CHECK (request_count BETWEEN 0 AND hard_limit)
);

INSERT INTO model_call_budgets (model, request_count, hard_limit)
VALUES ('gemini-3.7-flash', 0, 50);

CREATE OR REPLACE FUNCTION enforce_model_call_budget_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'model call budget rows are immutable except for reservations';
    END IF;
    IF NEW.model IS DISTINCT FROM OLD.model
        OR NEW.hard_limit IS DISTINCT FROM OLD.hard_limit
        OR NEW.request_count <> OLD.request_count + 1
    THEN
        RAISE EXCEPTION 'model call budget rows are immutable except for reservations';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER model_call_budget_update_guard
BEFORE UPDATE OR DELETE ON model_call_budgets
FOR EACH ROW EXECUTE FUNCTION enforce_model_call_budget_mutation();

CREATE TABLE evidence_classification_claims (
    evidence_hash varchar(64) PRIMARY KEY,
    state varchar(16) NOT NULL CHECK (state IN ('PENDING', 'COMPLETED')),
    generation bigint NOT NULL CHECK (generation > 0),
    lease_owner uuid UNIQUE,
    lease_expires_at timestamptz,
    updated_at timestamptz NOT NULL,
    CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
    CHECK (
        (state = 'PENDING' AND lease_expires_at IS NOT NULL)
        OR
        (state = 'COMPLETED' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE TABLE evidence_classifications (
    evidence_hash varchar(64) PRIMARY KEY
        REFERENCES evidence_classification_claims(evidence_hash),
    classifications_payload jsonb NOT NULL,
    classification_hash varchar(64) NOT NULL UNIQUE,
    completed_generation bigint NOT NULL CHECK (completed_generation > 0),
    completed_at timestamptz NOT NULL,
    CHECK (classification_hash ~ '^[0-9a-f]{64}$'),
    CHECK (jsonb_typeof(classifications_payload) = 'array'),
    CHECK (jsonb_array_length(classifications_payload) BETWEEN 1 AND 12)
);

CREATE OR REPLACE FUNCTION enforce_evidence_claim_transition()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'evidence ownership rows cannot be deleted';
    END IF;
    IF OLD.state = 'COMPLETED' THEN
        RAISE EXCEPTION 'completed evidence ownership is immutable';
    END IF;
    IF NEW.state = 'COMPLETED' THEN
        IF NEW.generation <> OLD.generation
            OR OLD.lease_owner IS NULL
            OR OLD.lease_expires_at <= clock_timestamp()
            OR NEW.lease_owner IS NOT NULL
            OR NEW.lease_expires_at IS NOT NULL
            OR NOT EXISTS (
                SELECT 1
                FROM evidence_classifications classification
                WHERE classification.evidence_hash = NEW.evidence_hash
                    AND classification.completed_generation = NEW.generation
            )
        THEN
            RAISE EXCEPTION 'invalid evidence completion transition';
        END IF;
    ELSIF NEW.generation <> OLD.generation + 1 THEN
        RAISE EXCEPTION 'invalid evidence ownership generation';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER evidence_classification_claim_transition_guard
BEFORE UPDATE OR DELETE ON evidence_classification_claims
FOR EACH ROW EXECUTE FUNCTION enforce_evidence_claim_transition();

CREATE OR REPLACE FUNCTION reject_evidence_classification_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    RAISE EXCEPTION 'completed evidence classifications are append-only';
END
$function$;

CREATE TRIGGER evidence_classifications_append_only
BEFORE UPDATE OR DELETE ON evidence_classifications
FOR EACH ROW EXECUTE FUNCTION reject_evidence_classification_mutation();
