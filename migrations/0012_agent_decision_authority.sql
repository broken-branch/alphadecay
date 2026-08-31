-- Persist agent inputs and decisions, permanently latch execution locks, and
-- leave the historical recovery schema inert. Migrations 0001-0011 remain anchors.

DROP TRIGGER IF EXISTS whole_account_reconciliations_append_only
    ON whole_account_reconciliations;

DO $migration$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT constraint_record.conname
        FROM pg_constraint AS constraint_record
        WHERE constraint_record.conrelid = 'whole_account_reconciliations'::regclass
          AND constraint_record.contype = 'c'
          AND pg_get_constraintdef(constraint_record.oid) ILIKE '%purpose%'
    LOOP
        EXECUTE format(
            'ALTER TABLE whole_account_reconciliations DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;
END
$migration$;

ALTER TABLE whole_account_reconciliations
    ALTER COLUMN purpose TYPE varchar(24);

UPDATE whole_account_reconciliations
SET purpose = 'BASELINE_INITIALIZATION'
WHERE purpose = 'LOCK_CLEAR'
  AND intent_digest = repeat('0', 64)
  AND request_hash = repeat('0', 64);

ALTER TABLE whole_account_reconciliations
    ADD CONSTRAINT ck_whole_reconciliation_purpose_v2 CHECK (
        purpose IN (
            'SUBMIT', 'REPLACE', 'CANCEL', 'BASELINE_INITIALIZATION', 'LOCK_CLEAR'
        )
    );

CREATE OR REPLACE FUNCTION reject_legacy_lock_clear_insert()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF NEW.purpose = 'LOCK_CLEAR' THEN
        RAISE EXCEPTION 'LOCK_CLEAR is disabled legacy authority';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER whole_account_reconciliation_legacy_purpose_guard
BEFORE INSERT ON whole_account_reconciliations
FOR EACH ROW EXECUTE FUNCTION reject_legacy_lock_clear_insert();

CREATE TRIGGER whole_account_reconciliations_append_only
BEFORE UPDATE OR DELETE ON whole_account_reconciliations
FOR EACH ROW EXECUTE FUNCTION reject_whole_account_authority_mutation();

CREATE TABLE agent_input_snapshots (
    snapshot_id uuid PRIMARY KEY,
    account_role varchar(16) NOT NULL REFERENCES account_roles(role),
    account_fingerprint varchar(64) NOT NULL
        CHECK (account_fingerprint ~ '^[0-9a-f]{64}$'),
    decision_kind varchar(16) NOT NULL
        CHECK (decision_kind IN ('OPPORTUNITY', 'ASSESSMENT')),
    decision_boundary timestamptz NOT NULL,
    observed_at timestamptz NOT NULL CHECK (observed_at >= decision_boundary),
    normalized_payload jsonb NOT NULL CHECK (jsonb_typeof(normalized_payload) = 'object'),
    input_hash varchar(64) NOT NULL UNIQUE CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    UNIQUE (account_role, decision_kind, decision_boundary)
);

CREATE TABLE agent_decisions (
    decision_id uuid PRIMARY KEY,
    origin_tick_id uuid NOT NULL,
    input_snapshot_id uuid NOT NULL UNIQUE REFERENCES agent_input_snapshots(snapshot_id),
    account_role varchar(16) NOT NULL REFERENCES account_roles(role),
    account_fingerprint varchar(64) NOT NULL
        CHECK (account_fingerprint ~ '^[0-9a-f]{64}$'),
    decision_kind varchar(16) NOT NULL
        CHECK (decision_kind IN ('OPPORTUNITY', 'ASSESSMENT')),
    outcome varchar(48) NOT NULL,
    reason_code varchar(64) NOT NULL,
    policy_hash varchar(64) NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    result_payload jsonb NOT NULL CHECK (jsonb_typeof(result_payload) = 'object'),
    result_hash varchar(64) NOT NULL UNIQUE CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    autonomy_authorized boolean NOT NULL,
    decision_boundary timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    CHECK (
        NOT (account_role = 'SUBMISSION' AND decision_kind = 'OPPORTUNITY')
        OR (
            outcome = 'NO_TRADE'
            AND reason_code = 'CALIBRATION_BINDING_NO_TRADE'
            AND NOT autonomy_authorized
        )
    )
);

CREATE TABLE agent_ticks (
    tick_id uuid PRIMARY KEY,
    account_role varchar(16) NOT NULL REFERENCES account_roles(role),
    account_fingerprint varchar(64) NOT NULL
        CHECK (account_fingerprint ~ '^[0-9a-f]{64}$'),
    tick_key varchar(128) NOT NULL,
    tick_boundary timestamptz NOT NULL,
    actor varchar(16) NOT NULL CHECK (actor IN ('OWNER', 'SCHEDULER')),
    status varchar(16) NOT NULL CHECK (status IN ('RESERVED', 'COMPLETED')),
    reservation_token uuid NOT NULL UNIQUE,
    terminal_code varchar(64),
    decision_id uuid REFERENCES agent_decisions(decision_id),
    execution_certificate_id uuid REFERENCES execution_certificates(certificate_id),
    proof_hash varchar(64),
    created_at timestamptz NOT NULL,
    completed_at timestamptz,
    UNIQUE (account_role, tick_key),
    CHECK (
        (status = 'RESERVED' AND terminal_code IS NULL AND proof_hash IS NULL
            AND completed_at IS NULL)
        OR
        (status = 'COMPLETED' AND terminal_code IS NOT NULL
            AND proof_hash ~ '^[0-9a-f]{64}$' AND completed_at IS NOT NULL
            AND completed_at >= created_at)
    )
);

ALTER TABLE agent_decisions
    ADD CONSTRAINT fk_agent_decision_origin_tick
    FOREIGN KEY (origin_tick_id) REFERENCES agent_ticks(tick_id);

ALTER TABLE entry_approval_certificates
    ADD COLUMN agent_decision_id uuid UNIQUE REFERENCES agent_decisions(decision_id);

ALTER TABLE assessment_certificates
    ADD COLUMN agent_decision_id uuid UNIQUE REFERENCES agent_decisions(decision_id);

CREATE OR REPLACE FUNCTION reject_agent_authority_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    RAISE EXCEPTION 'agent authority records are append-only';
END
$function$;

CREATE TRIGGER agent_input_snapshots_append_only
BEFORE UPDATE OR DELETE ON agent_input_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_agent_authority_mutation();

CREATE TRIGGER agent_decisions_append_only
BEFORE UPDATE OR DELETE ON agent_decisions
FOR EACH ROW EXECUTE FUNCTION reject_agent_authority_mutation();

CREATE OR REPLACE FUNCTION guard_agent_tick_transition()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'agent ticks cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - ARRAY[
            'status', 'terminal_code', 'decision_id', 'execution_certificate_id',
            'proof_hash', 'completed_at'
        ]) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY[
            'status', 'terminal_code', 'decision_id', 'execution_certificate_id',
            'proof_hash', 'completed_at'
        ])
    THEN
        RAISE EXCEPTION 'agent tick immutable fields changed';
    END IF;
    IF OLD.status = 'RESERVED' AND NEW.status = 'RESERVED'
        AND OLD.decision_id IS NULL AND NEW.decision_id IS NOT NULL
        AND NEW.terminal_code IS NULL
        AND NEW.execution_certificate_id IS NULL
        AND NEW.proof_hash IS NULL
        AND NEW.completed_at IS NULL
        AND EXISTS (
            SELECT 1 FROM agent_decisions
            WHERE decision_id = NEW.decision_id
              AND account_role = NEW.account_role
              AND account_fingerprint = NEW.account_fingerprint
              AND (NOT autonomy_authorized OR NEW.actor = 'SCHEDULER')
        )
    THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'RESERVED' AND NEW.status = 'COMPLETED'
        AND OLD.decision_id IS NOT NULL
        AND NEW.decision_id IS NOT DISTINCT FROM OLD.decision_id
        AND NEW.terminal_code IS NOT NULL
        AND NEW.proof_hash ~ '^[0-9a-f]{64}$'
        AND NEW.completed_at IS NOT NULL
        AND NEW.completed_at >= OLD.created_at
    THEN
        IF NEW.decision_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM agent_decisions
            WHERE decision_id = NEW.decision_id
              AND account_role = NEW.account_role
        ) THEN
            RAISE EXCEPTION 'agent tick decision mismatch';
        END IF;
        IF NEW.execution_certificate_id IS NOT NULL AND (
            NEW.decision_id IS NULL OR NOT EXISTS (
                SELECT 1
                FROM execution_certificates AS certificate
                JOIN execution_intents AS intent
                  ON intent.intent_id = certificate.execution_intent_id
                LEFT JOIN entry_approval_certificates AS entry_authorization
                  ON entry_authorization.approval_id = intent.entry_approval_id
                LEFT JOIN assessment_certificates AS assessment_authorization
                  ON assessment_authorization.certificate_id = intent.assessment_certificate_id
                WHERE certificate.certificate_id = NEW.execution_certificate_id
                  AND certificate.execution_status = NEW.terminal_code
                  AND COALESCE(
                      entry_authorization.agent_decision_id,
                      assessment_authorization.agent_decision_id
                  ) = NEW.decision_id
            )
        ) THEN
            RAISE EXCEPTION 'agent tick certificate mismatch';
        END IF;
        IF NEW.execution_certificate_id IS NULL AND NEW.terminal_code IN (
            'FILLED', 'REJECTED', 'CANCELED', 'EXPIRED',
            'PARTIAL_CANCELED_RECONCILED'
        ) THEN
            RAISE EXCEPTION 'agent tick execution terminal requires certificate';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'agent tick transition invalid';
END
$function$;

CREATE TRIGGER agent_ticks_append_only
BEFORE UPDATE OR DELETE ON agent_ticks
FOR EACH ROW EXECUTE FUNCTION guard_agent_tick_transition();

CREATE OR REPLACE FUNCTION validate_agent_decision_insert()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    input_record agent_input_snapshots%ROWTYPE;
    origin_tick_record agent_ticks%ROWTYPE;
    account_fingerprint_value varchar(64);
BEGIN
    SELECT * INTO input_record
    FROM agent_input_snapshots
    WHERE snapshot_id = NEW.input_snapshot_id;

    SELECT * INTO origin_tick_record
    FROM agent_ticks
    WHERE tick_id = NEW.origin_tick_id;

    SELECT account_fingerprint INTO account_fingerprint_value
    FROM account_roles
    WHERE role = NEW.account_role;

    IF input_record.snapshot_id IS NULL
        OR input_record.account_role IS DISTINCT FROM NEW.account_role
        OR input_record.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
        OR input_record.decision_kind IS DISTINCT FROM NEW.decision_kind
        OR input_record.decision_boundary IS DISTINCT FROM NEW.decision_boundary
        OR origin_tick_record.tick_id IS NULL
        OR origin_tick_record.status IS DISTINCT FROM 'RESERVED'
        OR origin_tick_record.account_role IS DISTINCT FROM NEW.account_role
        OR origin_tick_record.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
        OR account_fingerprint_value IS DISTINCT FROM NEW.account_fingerprint
        OR (
            NEW.account_role = 'SUBMISSION'
            AND NEW.decision_kind = 'OPPORTUNITY'
            AND (
                COALESCE(input_record.normalized_payload ->> 'machine_binding_hash', '')
                    !~ '^[0-9a-f]{64}$'
                OR COALESCE(input_record.normalized_payload ->> 'calibration_hash', '')
                    !~ '^[0-9a-f]{64}$'
            )
        )
    THEN
        RAISE EXCEPTION 'agent decision lineage mismatch';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER agent_decision_insert_guard
BEFORE INSERT ON agent_decisions
FOR EACH ROW EXECUTE FUNCTION validate_agent_decision_insert();

CREATE OR REPLACE FUNCTION validate_agent_decision_authority_marker()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM agent_ticks
        WHERE tick_id = NEW.origin_tick_id
          AND decision_id = NEW.decision_id
          AND account_role = NEW.account_role
          AND account_fingerprint = NEW.account_fingerprint
    ) THEN
        RAISE EXCEPTION 'agent decision origin tick mismatch';
    END IF;
    IF NEW.autonomy_authorized AND NOT EXISTS (
        SELECT 1 FROM entry_approval_certificates
        WHERE agent_decision_id = NEW.decision_id
        UNION ALL
        SELECT 1 FROM assessment_certificates
        WHERE agent_decision_id = NEW.decision_id
    ) THEN
        RAISE EXCEPTION 'autonomy-authorized decision requires exact authorization';
    END IF;
    RETURN NEW;
END
$function$;

CREATE CONSTRAINT TRIGGER agent_decision_authority_marker_guard
AFTER INSERT ON agent_decisions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_agent_decision_authority_marker();

CREATE OR REPLACE FUNCTION validate_agent_tick_insert()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    account_fingerprint_value varchar(64);
BEGIN
    SELECT account_fingerprint INTO account_fingerprint_value
    FROM account_roles
    WHERE role = NEW.account_role;
    IF account_fingerprint_value IS DISTINCT FROM NEW.account_fingerprint
        OR NEW.status IS DISTINCT FROM 'RESERVED'
        OR NEW.tick_boundary > CURRENT_TIMESTAMP
        OR mod(extract(epoch FROM NEW.tick_boundary)::bigint, 300) <> 0
        OR NEW.terminal_code IS NOT NULL
        OR NEW.decision_id IS NOT NULL
        OR NEW.execution_certificate_id IS NOT NULL
        OR NEW.proof_hash IS NOT NULL
        OR NEW.completed_at IS NOT NULL
    THEN
        RAISE EXCEPTION 'agent tick reservation invalid';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER agent_tick_insert_guard
BEFORE INSERT ON agent_ticks
FOR EACH ROW EXECUTE FUNCTION validate_agent_tick_insert();

CREATE OR REPLACE FUNCTION guard_linked_authorization_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.agent_decision_id IS NOT NULL THEN
            RAISE EXCEPTION 'agent-linked authorization is immutable';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.agent_decision_id IS NOT NULL
        OR NEW.agent_decision_id IS DISTINCT FROM OLD.agent_decision_id THEN
        RAISE EXCEPTION 'agent-linked authorization is immutable';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER linked_entry_authorization_guard
BEFORE UPDATE OR DELETE ON entry_approval_certificates
FOR EACH ROW EXECUTE FUNCTION guard_linked_authorization_mutation();

CREATE TRIGGER linked_assessment_authorization_guard
BEFORE UPDATE OR DELETE ON assessment_certificates
FOR EACH ROW EXECUTE FUNCTION guard_linked_authorization_mutation();

CREATE OR REPLACE FUNCTION validate_linked_authorization_has_intent()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF NEW.agent_decision_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'entry_approval_certificates' AND NOT EXISTS (
        SELECT 1 FROM execution_intents
        WHERE entry_approval_id = (to_jsonb(NEW) ->> 'approval_id')::uuid
    ) THEN
        RAISE EXCEPTION 'agent-linked entry authorization requires exact intent';
    END IF;
    IF TG_TABLE_NAME = 'assessment_certificates' AND NOT EXISTS (
        SELECT 1 FROM execution_intents
        WHERE assessment_certificate_id = (to_jsonb(NEW) ->> 'certificate_id')::uuid
    ) THEN
        RAISE EXCEPTION 'agent-linked assessment authorization requires exact intent';
    END IF;
    RETURN NEW;
END
$function$;

CREATE CONSTRAINT TRIGGER linked_entry_authorization_intent_guard
AFTER INSERT ON entry_approval_certificates
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_linked_authorization_has_intent();

CREATE CONSTRAINT TRIGGER linked_assessment_authorization_intent_guard
AFTER INSERT ON assessment_certificates
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_linked_authorization_has_intent();

CREATE OR REPLACE FUNCTION validate_agent_linked_intent_insert()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    decision_record agent_decisions%ROWTYPE;
    entry_record entry_approval_certificates%ROWTYPE;
    assessment_record assessment_certificates%ROWTYPE;
    account_record account_roles%ROWTYPE;
BEGIN
    IF NEW.entry_approval_id IS NOT NULL THEN
        SELECT * INTO entry_record
        FROM entry_approval_certificates
        WHERE approval_id = NEW.entry_approval_id;
        IF entry_record.agent_decision_id IS NULL THEN
            RETURN NEW;
        END IF;
        SELECT * INTO decision_record
        FROM agent_decisions
        WHERE decision_id = entry_record.agent_decision_id;
        SELECT * INTO account_record
        FROM account_roles WHERE role = NEW.account_role FOR UPDATE;
        IF NOT decision_record.autonomy_authorized
            OR NOT account_record.autonomous_enabled
            OR account_record.execution_locked
            OR account_record.recovery_pending
            OR decision_record.account_role IS DISTINCT FROM NEW.account_role
            OR decision_record.account_fingerprint
                IS DISTINCT FROM account_record.account_fingerprint
            OR decision_record.policy_hash IS DISTINCT FROM NEW.policy_hash
            OR decision_record.decision_kind IS DISTINCT FROM 'OPPORTUNITY'
            OR decision_record.outcome IS DISTINCT FROM 'ENTRY_APPROVED'
            OR NEW.action IS DISTINCT FROM 'ENTRY'
            OR NOT EXISTS (
                SELECT 1 FROM agent_ticks
                WHERE tick_id = decision_record.origin_tick_id
                  AND actor = 'SCHEDULER'
            )
            OR entry_record.account_role IS DISTINCT FROM NEW.account_role
            OR entry_record.policy_hash IS DISTINCT FROM NEW.policy_hash
            OR entry_record.book_fingerprint IS DISTINCT FROM NEW.fingerprint
            OR entry_record.envelope_hash IS DISTINCT FROM NEW.envelope_hash
            OR entry_record.approved_max_loss IS DISTINCT FROM NEW.approved_max_loss
            OR entry_record.quantity IS DISTINCT FROM NEW.quantity
            OR NEW.envelope_payload ->> 'account_fingerprint'
                IS DISTINCT FROM decision_record.account_fingerprint
            OR NEW.envelope_payload ->> 'authorization_certificate_id'
                IS DISTINCT FROM entry_record.approval_id::text
        THEN
            RAISE EXCEPTION 'agent-linked entry intent mismatch';
        END IF;
    ELSE
        SELECT * INTO assessment_record
        FROM assessment_certificates
        WHERE certificate_id = NEW.assessment_certificate_id;
        IF assessment_record.agent_decision_id IS NULL THEN
            RETURN NEW;
        END IF;
        SELECT * INTO decision_record
        FROM agent_decisions
        WHERE decision_id = assessment_record.agent_decision_id;
        SELECT * INTO account_record
        FROM account_roles WHERE role = NEW.account_role FOR UPDATE;
        IF NOT decision_record.autonomy_authorized
            OR NOT account_record.autonomous_enabled
            OR account_record.execution_locked
            OR account_record.recovery_pending
            OR decision_record.account_role IS DISTINCT FROM NEW.account_role
            OR decision_record.account_fingerprint
                IS DISTINCT FROM account_record.account_fingerprint
            OR decision_record.policy_hash IS DISTINCT FROM NEW.policy_hash
            OR decision_record.decision_kind IS DISTINCT FROM 'ASSESSMENT'
            OR (
                NEW.action = 'CLOSE'
                AND decision_record.outcome NOT IN ('CLOSE_APPROVED', 'CLOSE_RISK_ONLY')
            )
            OR (
                NEW.action = 'ROLL'
                AND decision_record.outcome IS DISTINCT FROM 'ROLL_APPROVED'
            )
            OR NEW.action NOT IN ('CLOSE', 'ROLL')
            OR NOT EXISTS (
                SELECT 1 FROM agent_ticks
                WHERE tick_id = decision_record.origin_tick_id
                  AND actor = 'SCHEDULER'
            )
            OR assessment_record.account_role IS DISTINCT FROM NEW.account_role
            OR assessment_record.policy_hash IS DISTINCT FROM NEW.policy_hash
            OR assessment_record.action IS DISTINCT FROM NEW.action
            OR assessment_record.position_fingerprint IS DISTINCT FROM NEW.fingerprint
            OR assessment_record.envelope_hash IS DISTINCT FROM NEW.envelope_hash
            OR assessment_record.approved_max_loss IS DISTINCT FROM NEW.approved_max_loss
            OR assessment_record.quantity IS DISTINCT FROM NEW.quantity
            OR NEW.envelope_payload ->> 'account_fingerprint'
                IS DISTINCT FROM decision_record.account_fingerprint
            OR NEW.envelope_payload ->> 'authorization_certificate_id'
                IS DISTINCT FROM assessment_record.certificate_id::text
        THEN
            RAISE EXCEPTION 'agent-linked assessment intent mismatch';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER agent_linked_intent_insert_guard
BEFORE INSERT ON execution_intents
FOR EACH ROW EXECUTE FUNCTION validate_agent_linked_intent_insert();

CREATE OR REPLACE FUNCTION guard_agent_linked_intent_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    is_agent_linked boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM entry_approval_certificates
        WHERE approval_id = OLD.entry_approval_id
          AND agent_decision_id IS NOT NULL
        UNION ALL
        SELECT 1
        FROM assessment_certificates
        WHERE certificate_id = OLD.assessment_certificate_id
          AND agent_decision_id IS NOT NULL
    ) INTO is_agent_linked;

    IF is_agent_linked AND TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'agent-linked intent cannot be deleted';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF is_agent_linked AND (
        to_jsonb(NEW) - ARRAY[
            'state', 'claimed_by', 'claimed_at', 'claim_token', 'claim_generation',
            'execution_epoch', 'heartbeat_at', 'lease_expires_at', 'first_fill_consumed'
        ]
    ) IS DISTINCT FROM (
        to_jsonb(OLD) - ARRAY[
            'state', 'claimed_by', 'claimed_at', 'claim_token', 'claim_generation',
            'execution_epoch', 'heartbeat_at', 'lease_expires_at', 'first_fill_consumed'
        ]
    ) THEN
        RAISE EXCEPTION 'agent-linked intent immutable fields changed';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER agent_linked_intent_mutation_guard
BEFORE UPDATE OR DELETE ON execution_intents
FOR EACH ROW EXECUTE FUNCTION guard_agent_linked_intent_mutation();

CREATE OR REPLACE FUNCTION guard_permanent_account_latch()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'account authority rows cannot be deleted';
    END IF;
    IF NEW.recovery_pending THEN
        RAISE EXCEPTION 'account recovery is permanently disabled';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.role IS DISTINCT FROM OLD.role
        OR NEW.account_fingerprint IS DISTINCT FROM OLD.account_fingerprint
    ) THEN
        RAISE EXCEPTION 'account identity is immutable';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.execution_locked AND (
        NOT NEW.execution_locked
        OR NEW.execution_lock_reason IS DISTINCT FROM OLD.execution_lock_reason
        OR NEW.execution_locked_at IS DISTINCT FROM OLD.execution_locked_at
        OR NEW.execution_lock_id IS DISTINCT FROM OLD.execution_lock_id
        OR NEW.execution_lock_generation IS DISTINCT FROM OLD.execution_lock_generation
    ) THEN
        RAISE EXCEPTION 'account execution lock is permanent';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER permanent_account_latch_guard
BEFORE INSERT OR UPDATE OR DELETE ON account_roles
FOR EACH ROW EXECUTE FUNCTION guard_permanent_account_latch();

CREATE OR REPLACE FUNCTION reject_legacy_recovery_dml()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    RAISE EXCEPTION 'account recovery is permanently disabled';
END
$function$;

DROP TRIGGER IF EXISTS recovery_cases_disabled ON recovery_cases;
CREATE TRIGGER recovery_cases_disabled
BEFORE INSERT OR UPDATE OR DELETE ON recovery_cases
FOR EACH ROW EXECUTE FUNCTION reject_legacy_recovery_dml();

DROP TRIGGER IF EXISTS recovery_events_disabled ON recovery_events;
CREATE TRIGGER recovery_events_disabled
BEFORE INSERT OR UPDATE OR DELETE ON recovery_events
FOR EACH ROW EXECUTE FUNCTION reject_legacy_recovery_dml();

DROP TRIGGER IF EXISTS recovery_certificates_disabled ON recovery_certificates;
CREATE TRIGGER recovery_certificates_disabled
BEFORE INSERT OR UPDATE OR DELETE ON recovery_certificates
FOR EACH ROW EXECUTE FUNCTION reject_legacy_recovery_dml();

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON recovery_cases FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON recovery_events FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON recovery_certificates FROM PUBLIC;
