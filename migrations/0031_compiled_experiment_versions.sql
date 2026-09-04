CREATE TABLE compiled_experiment_versions (
    experiment_id uuid PRIMARY KEY
        REFERENCES reviewed_experiment_definitions(experiment_id) ON DELETE RESTRICT,
    source_version integer NOT NULL CHECK (source_version = 1),
    compiled_version integer NOT NULL CHECK (compiled_version = 1),
    source_definition_hash varchar(64) NOT NULL
        CHECK (source_definition_hash ~ '^[0-9a-f]{64}$'),
    protocol_hash varchar(64) NOT NULL
        CHECK (protocol_hash ~ '^[0-9a-f]{64}$'),
    lifecycle_state varchar(16) NOT NULL CHECK (lifecycle_state = 'COMPILED'),
    arm_state varchar(16) NOT NULL CHECK (arm_state = 'NOT_ARMED'),
    automation_state varchar(16) NOT NULL CHECK (automation_state = 'OFF'),
    execution_eligible boolean NOT NULL CHECK (execution_eligible = false),
    payload_text text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE FUNCTION enforce_compiled_experiment_source_binding()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM reviewed_experiment_definitions source
        WHERE source.experiment_id = NEW.experiment_id
          AND source.version = NEW.source_version
          AND source.definition_hash = NEW.source_definition_hash
    ) THEN
        RAISE EXCEPTION 'compiled experiment source binding is invalid';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER compiled_experiment_versions_source_binding
BEFORE INSERT ON compiled_experiment_versions
FOR EACH ROW EXECUTE FUNCTION enforce_compiled_experiment_source_binding();

CREATE FUNCTION reject_compiled_experiment_version_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    RAISE EXCEPTION 'compiled experiment versions are immutable';
END
$function$;

CREATE TRIGGER compiled_experiment_versions_no_update
BEFORE UPDATE ON compiled_experiment_versions
FOR EACH ROW EXECUTE FUNCTION reject_compiled_experiment_version_mutation();

CREATE TRIGGER compiled_experiment_versions_no_delete
BEFORE DELETE ON compiled_experiment_versions
FOR EACH ROW EXECUTE FUNCTION reject_compiled_experiment_version_mutation();
