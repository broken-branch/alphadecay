ALTER TABLE compiled_experiment_versions
    ADD CONSTRAINT uq_compiled_experiment_authorization_identity
    UNIQUE (experiment_id, source_definition_hash, protocol_hash);

CREATE TABLE experiment_arm_events (
    event_id uuid PRIMARY KEY,
    experiment_id uuid NOT NULL,
    source_definition_hash varchar(64) NOT NULL
        CHECK (source_definition_hash ~ '^[0-9a-f]{64}$'),
    protocol_hash varchar(64) NOT NULL CHECK (protocol_hash ~ '^[0-9a-f]{64}$'),
    authorization_revision integer NOT NULL CHECK (authorization_revision > 0),
    action varchar(16) NOT NULL CHECK (action IN ('ARM', 'DISARM')),
    authorization_state varchar(16) NOT NULL
        CHECK (authorization_state IN ('ARMED', 'DISARMED')),
    CHECK (
        (action = 'ARM' AND authorization_state = 'ARMED')
        OR (action = 'DISARM' AND authorization_state = 'DISARMED')
    ),
    entry_authorized boolean NOT NULL
        CHECK (entry_authorized = (authorization_state = 'ARMED')),
    existing_position_risk_management_preserved boolean NOT NULL CHECK (
        existing_position_risk_management_preserved = true
    ),
    runtime_state varchar(16) NOT NULL CHECK (runtime_state = 'NOT_CONNECTED'),
    execution_eligible boolean NOT NULL CHECK (execution_eligible = false),
    paper_trading_only boolean NOT NULL CHECK (paper_trading_only = true),
    event_hash varchar(64) NOT NULL UNIQUE CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    CONSTRAINT fk_experiment_arm_event_compiled_identity
        FOREIGN KEY (experiment_id, source_definition_hash, protocol_hash)
        REFERENCES compiled_experiment_versions
            (experiment_id, source_definition_hash, protocol_hash)
        ON DELETE RESTRICT,
    CONSTRAINT uq_experiment_arm_event_revision
        UNIQUE (experiment_id, authorization_revision),
    CONSTRAINT uq_experiment_arm_event_state_binding
        UNIQUE (
            experiment_id,
            source_definition_hash,
            protocol_hash,
            authorization_revision,
            authorization_state,
            entry_authorized,
            event_hash,
            created_at
        )
);

CREATE INDEX ix_experiment_arm_events_experiment
    ON experiment_arm_events (experiment_id, authorization_revision);

CREATE TABLE experiment_arm_states (
    experiment_id uuid PRIMARY KEY,
    source_definition_hash varchar(64) NOT NULL
        CHECK (source_definition_hash ~ '^[0-9a-f]{64}$'),
    protocol_hash varchar(64) NOT NULL CHECK (protocol_hash ~ '^[0-9a-f]{64}$'),
    authorization_revision integer NOT NULL CHECK (authorization_revision > 0),
    authorization_state varchar(16) NOT NULL
        CHECK (authorization_state IN ('ARMED', 'DISARMED')),
    entry_authorized boolean NOT NULL
        CHECK (entry_authorized = (authorization_state = 'ARMED')),
    existing_position_risk_management_preserved boolean NOT NULL CHECK (
        existing_position_risk_management_preserved = true
    ),
    runtime_state varchar(16) NOT NULL CHECK (runtime_state = 'NOT_CONNECTED'),
    execution_eligible boolean NOT NULL CHECK (execution_eligible = false),
    paper_trading_only boolean NOT NULL CHECK (paper_trading_only = true),
    last_event_hash varchar(64) NOT NULL CHECK (last_event_hash ~ '^[0-9a-f]{64}$'),
    updated_at timestamptz NOT NULL,
    CONSTRAINT fk_experiment_arm_state_compiled_identity
        FOREIGN KEY (experiment_id, source_definition_hash, protocol_hash)
        REFERENCES compiled_experiment_versions
            (experiment_id, source_definition_hash, protocol_hash)
        ON DELETE RESTRICT,
    CONSTRAINT fk_experiment_arm_state_current_event
        FOREIGN KEY (
            experiment_id,
            source_definition_hash,
            protocol_hash,
            authorization_revision,
            authorization_state,
            entry_authorized,
            last_event_hash,
            updated_at
        )
        REFERENCES experiment_arm_events (
            experiment_id,
            source_definition_hash,
            protocol_hash,
            authorization_revision,
            authorization_state,
            entry_authorized,
            event_hash,
            created_at
        )
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_one_armed_experiment
    ON experiment_arm_states (authorization_state)
    WHERE authorization_state = 'ARMED';

CREATE FUNCTION guard_experiment_arm_event_insert()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    current_state experiment_arm_states%ROWTYPE;
BEGIN
    SELECT * INTO current_state
      FROM experiment_arm_states
     WHERE experiment_id = NEW.experiment_id
     FOR UPDATE;
    IF NOT FOUND THEN
        IF NEW.authorization_revision <> 1 OR NEW.action <> 'ARM' THEN
            RAISE EXCEPTION 'invalid initial experiment arm event';
        END IF;
    ELSIF NEW.authorization_revision <> current_state.authorization_revision + 1
       OR NEW.authorization_state = current_state.authorization_state
       OR NEW.created_at < current_state.updated_at THEN
        RAISE EXCEPTION 'invalid experiment arm event transition';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER experiment_arm_events_insert_guard
BEFORE INSERT ON experiment_arm_events
FOR EACH ROW EXECUTE FUNCTION guard_experiment_arm_event_insert();

CREATE FUNCTION guard_experiment_arm_state_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'experiment arm states cannot be deleted';
    END IF;
    IF NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
       OR NEW.source_definition_hash IS DISTINCT FROM OLD.source_definition_hash
       OR NEW.protocol_hash IS DISTINCT FROM OLD.protocol_hash
       OR NEW.authorization_revision <> OLD.authorization_revision + 1
       OR NEW.authorization_state = OLD.authorization_state
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'invalid experiment arm state transition';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER experiment_arm_states_guard
BEFORE UPDATE OR DELETE ON experiment_arm_states
FOR EACH ROW EXECUTE FUNCTION guard_experiment_arm_state_mutation();

CREATE FUNCTION enforce_experiment_arm_event_state_binding()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    current_state experiment_arm_states%ROWTYPE;
BEGIN
    SELECT * INTO current_state
      FROM experiment_arm_states
     WHERE experiment_id = NEW.experiment_id;
    IF NOT FOUND
       OR current_state.authorization_revision < NEW.authorization_revision
       OR (
           current_state.authorization_revision = NEW.authorization_revision
           AND (
               current_state.source_definition_hash <> NEW.source_definition_hash
               OR current_state.protocol_hash <> NEW.protocol_hash
               OR current_state.authorization_state <> NEW.authorization_state
               OR current_state.entry_authorized <> NEW.entry_authorized
               OR current_state.last_event_hash <> NEW.event_hash
               OR current_state.updated_at <> NEW.created_at
           )
       ) THEN
        RAISE EXCEPTION 'experiment arm event is not bound to current state';
    END IF;
    RETURN NULL;
END
$function$;

CREATE CONSTRAINT TRIGGER experiment_arm_event_state_binding
AFTER INSERT ON experiment_arm_events
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION enforce_experiment_arm_event_state_binding();

CREATE FUNCTION reject_experiment_arm_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    RAISE EXCEPTION 'experiment arm events are immutable';
END
$function$;

CREATE TRIGGER experiment_arm_events_no_update
BEFORE UPDATE ON experiment_arm_events
FOR EACH ROW EXECUTE FUNCTION reject_experiment_arm_event_mutation();

CREATE TRIGGER experiment_arm_events_no_delete
BEFORE DELETE ON experiment_arm_events
FOR EACH ROW EXECUTE FUNCTION reject_experiment_arm_event_mutation();
