CREATE TABLE reviewed_experiment_definitions (
    experiment_id uuid PRIMARY KEY,
    version integer NOT NULL CHECK (version = 1),
    definition_hash varchar(64) NOT NULL UNIQUE
        CHECK (definition_hash ~ '^[0-9a-f]{64}$'),
    lifecycle_state varchar(16) NOT NULL CHECK (lifecycle_state = 'REVIEWED'),
    payload_text text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX ix_reviewed_experiment_definitions_created_at
    ON reviewed_experiment_definitions (created_at DESC, experiment_id DESC);

CREATE FUNCTION reject_reviewed_experiment_definition_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    RAISE EXCEPTION 'reviewed experiment definitions are immutable';
END
$function$;

CREATE TRIGGER reviewed_experiment_definitions_no_update
BEFORE UPDATE ON reviewed_experiment_definitions
FOR EACH ROW EXECUTE FUNCTION reject_reviewed_experiment_definition_mutation();

CREATE TRIGGER reviewed_experiment_definitions_no_delete
BEFORE DELETE ON reviewed_experiment_definitions
FOR EACH ROW EXECUTE FUNCTION reject_reviewed_experiment_definition_mutation();
