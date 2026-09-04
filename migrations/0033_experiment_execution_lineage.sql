ALTER TABLE agent_decisions
    ADD COLUMN experiment_id uuid,
    ADD COLUMN experiment_source_definition_hash varchar(64),
    ADD COLUMN experiment_protocol_hash varchar(64),
    ADD CONSTRAINT ck_agent_decision_experiment_lineage CHECK (
        (experiment_id IS NULL
            AND experiment_source_definition_hash IS NULL
            AND experiment_protocol_hash IS NULL)
        OR
        (experiment_id IS NOT NULL
            AND experiment_source_definition_hash IS NOT NULL
            AND experiment_protocol_hash IS NOT NULL)
    ),
    ADD CONSTRAINT uq_agent_decision_experiment_identity UNIQUE (
        decision_id,
        experiment_id,
        experiment_source_definition_hash,
        experiment_protocol_hash
    ),
    ADD CONSTRAINT fk_agent_decision_experiment_compiled FOREIGN KEY (
        experiment_id,
        experiment_source_definition_hash,
        experiment_protocol_hash
    ) REFERENCES compiled_experiment_versions (
        experiment_id,
        source_definition_hash,
        protocol_hash
    ) MATCH FULL ON DELETE RESTRICT;

ALTER TABLE entry_approval_certificates
    ADD COLUMN experiment_id uuid,
    ADD COLUMN experiment_source_definition_hash varchar(64),
    ADD COLUMN experiment_protocol_hash varchar(64),
    ADD CONSTRAINT ck_entry_approval_experiment_lineage CHECK (
        (experiment_id IS NULL
            AND experiment_source_definition_hash IS NULL
            AND experiment_protocol_hash IS NULL)
        OR
        (experiment_id IS NOT NULL
            AND agent_decision_id IS NOT NULL
            AND experiment_source_definition_hash IS NOT NULL
            AND experiment_protocol_hash IS NOT NULL)
    ),
    ADD CONSTRAINT uq_entry_approval_experiment_identity UNIQUE (
        approval_id,
        experiment_id,
        experiment_source_definition_hash,
        experiment_protocol_hash
    ),
    ADD CONSTRAINT fk_entry_approval_experiment_decision FOREIGN KEY (
        agent_decision_id,
        experiment_id,
        experiment_source_definition_hash,
        experiment_protocol_hash
    ) REFERENCES agent_decisions (
        decision_id,
        experiment_id,
        experiment_source_definition_hash,
        experiment_protocol_hash
    ) ON DELETE RESTRICT;

ALTER TABLE assessment_certificates
    ADD COLUMN experiment_id uuid,
    ADD COLUMN experiment_source_definition_hash varchar(64),
    ADD COLUMN experiment_protocol_hash varchar(64),
    ADD CONSTRAINT ck_assessment_experiment_lineage CHECK (
        (experiment_id IS NULL
            AND experiment_source_definition_hash IS NULL
            AND experiment_protocol_hash IS NULL)
        OR
        (experiment_id IS NOT NULL
            AND agent_decision_id IS NOT NULL
            AND experiment_source_definition_hash IS NOT NULL
            AND experiment_protocol_hash IS NOT NULL)
    ),
    ADD CONSTRAINT fk_assessment_experiment_decision FOREIGN KEY (
        agent_decision_id,
        experiment_id,
        experiment_source_definition_hash,
        experiment_protocol_hash
    ) REFERENCES agent_decisions (
        decision_id,
        experiment_id,
        experiment_source_definition_hash,
        experiment_protocol_hash
    ) ON DELETE RESTRICT;

ALTER TABLE managed_lifecycle_positions
    ADD COLUMN experiment_id uuid,
    ADD COLUMN experiment_source_definition_hash varchar(64),
    ADD COLUMN experiment_protocol_hash varchar(64),
    ADD CONSTRAINT ck_managed_position_experiment_lineage CHECK (
        (experiment_id IS NULL
            AND experiment_source_definition_hash IS NULL
            AND experiment_protocol_hash IS NULL)
        OR
        (experiment_id IS NOT NULL
            AND experiment_source_definition_hash IS NOT NULL
            AND experiment_protocol_hash IS NOT NULL)
    ),
    ADD CONSTRAINT fk_managed_position_experiment_approval FOREIGN KEY (
        entry_approval_id,
        experiment_id,
        experiment_source_definition_hash,
        experiment_protocol_hash
    ) REFERENCES entry_approval_certificates (
        approval_id,
        experiment_id,
        experiment_source_definition_hash,
        experiment_protocol_hash
    ) ON DELETE RESTRICT;

CREATE FUNCTION experiment_authorization_decision_lineage_guard()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    decision agent_decisions%ROWTYPE;
BEGIN
    IF NEW.agent_decision_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT * INTO decision
    FROM agent_decisions
    WHERE decision_id = NEW.agent_decision_id;
    IF decision.decision_id IS NULL
        OR decision.experiment_id IS DISTINCT FROM NEW.experiment_id
        OR decision.experiment_source_definition_hash
            IS DISTINCT FROM NEW.experiment_source_definition_hash
        OR decision.experiment_protocol_hash
            IS DISTINCT FROM NEW.experiment_protocol_hash
    THEN
        RAISE EXCEPTION 'EXPERIMENT_AUTHORIZATION_DECISION_LINEAGE_INVALID';
    END IF;
    RETURN NEW;
END
$function$;

CREATE CONSTRAINT TRIGGER experiment_entry_approval_decision_lineage_guard
AFTER INSERT ON entry_approval_certificates
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION experiment_authorization_decision_lineage_guard();

CREATE CONSTRAINT TRIGGER experiment_assessment_decision_lineage_guard
AFTER INSERT ON assessment_certificates
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION experiment_authorization_decision_lineage_guard();

CREATE FUNCTION experiment_managed_position_lineage_guard()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    approval entry_approval_certificates%ROWTYPE;
BEGIN
    SELECT * INTO approval
    FROM entry_approval_certificates
    WHERE approval_id = NEW.entry_approval_id;
    IF approval.approval_id IS NULL
        OR approval.experiment_id IS DISTINCT FROM NEW.experiment_id
        OR approval.experiment_source_definition_hash
            IS DISTINCT FROM NEW.experiment_source_definition_hash
        OR approval.experiment_protocol_hash
            IS DISTINCT FROM NEW.experiment_protocol_hash
    THEN
        RAISE EXCEPTION 'EXPERIMENT_MANAGED_POSITION_LINEAGE_INVALID';
    END IF;
    RETURN NEW;
END
$function$;

CREATE CONSTRAINT TRIGGER experiment_managed_position_lineage_guard
AFTER INSERT ON managed_lifecycle_positions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION experiment_managed_position_lineage_guard();

CREATE FUNCTION experiment_assessment_position_lineage_guard()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    position managed_lifecycle_positions%ROWTYPE;
BEGIN
    SELECT * INTO position
    FROM managed_lifecycle_positions
    WHERE account_role = NEW.account_role
      AND active_position_fingerprint = NEW.position_fingerprint
      AND closed_at IS NULL;
    IF position.managed_position_id IS NULL
        OR position.experiment_id IS DISTINCT FROM NEW.experiment_id
        OR position.experiment_source_definition_hash
            IS DISTINCT FROM NEW.experiment_source_definition_hash
        OR position.experiment_protocol_hash
            IS DISTINCT FROM NEW.experiment_protocol_hash
    THEN
        RAISE EXCEPTION 'EXPERIMENT_ASSESSMENT_POSITION_LINEAGE_INVALID';
    END IF;
    RETURN NEW;
END
$function$;

CREATE CONSTRAINT TRIGGER experiment_assessment_position_lineage_guard
AFTER INSERT ON assessment_certificates
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION experiment_assessment_position_lineage_guard();
