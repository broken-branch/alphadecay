-- Preserve the filled execution certificate when managed-lifecycle
-- materialization fails after the broker result is final.

CREATE TABLE entry_materialization_jobs (
    execution_intent_id uuid PRIMARY KEY REFERENCES execution_intents(intent_id),
    entry_approval_id uuid NOT NULL UNIQUE REFERENCES entry_approval_certificates(approval_id),
    account_role varchar(16) NOT NULL REFERENCES account_roles(role)
        CHECK (account_role = 'DEVELOPMENT'),
    account_fingerprint varchar(64) NOT NULL
        CHECK (account_fingerprint ~ '^[0-9a-f]{64}$'),
    beta60 numeric(18,8) NOT NULL CHECK (beta60 > 0 AND beta60 <= 3),
    benchmark_symbol varchar(6) NOT NULL CHECK (benchmark_symbol = 'QQQ'),
    entry_boundary_at timestamptz NOT NULL,
    entry_policy_hash varchar(64) NOT NULL CHECK (entry_policy_hash ~ '^[0-9a-f]{64}$'),
    underlying_source_hash varchar(64) NOT NULL
        CHECK (underlying_source_hash ~ '^[0-9a-f]{64}$'),
    benchmark_source_hash varchar(64) NOT NULL
        CHECK (benchmark_source_hash ~ '^[0-9a-f]{64}$'),
    completed_bar_source_hash varchar(64) NOT NULL
        CHECK (completed_bar_source_hash ~ '^[0-9a-f]{64}$'),
    job_hash varchar(64) NOT NULL UNIQUE CHECK (job_hash ~ '^[0-9a-f]{64}$'),
    prepared_at timestamptz NOT NULL CHECK (prepared_at >= entry_boundary_at),
    managed_position_id uuid UNIQUE REFERENCES managed_lifecycle_positions(managed_position_id),
    terminal_status varchar(40) CHECK (terminal_status IN (
        'FILLED','REJECTED','CANCELED','EXPIRED','PARTIAL_CANCELED_RECONCILED'
    )),
    completed_at timestamptz,
    CHECK ((terminal_status IS NULL) = (completed_at IS NULL)),
    CHECK ((terminal_status = 'FILLED') = (managed_position_id IS NOT NULL)),
    CHECK (completed_at IS NULL OR completed_at >= prepared_at)
);

CREATE FUNCTION entry_materialization_job_insert_guard()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE intent execution_intents%ROWTYPE;
DECLARE approval entry_approval_certificates%ROWTYPE;
DECLARE account account_roles%ROWTYPE;
DECLARE decision agent_decisions%ROWTYPE;
DECLARE snapshot agent_input_snapshots%ROWTYPE;
DECLARE thesis thesis_versions%ROWTYPE;
DECLARE expected_job_hash varchar(64);
BEGIN
    SELECT * INTO intent FROM execution_intents WHERE intent_id=NEW.execution_intent_id;
    SELECT * INTO approval FROM entry_approval_certificates WHERE approval_id=NEW.entry_approval_id;
    SELECT * INTO account FROM account_roles WHERE role=NEW.account_role;
    SELECT * INTO decision FROM agent_decisions WHERE decision_id=approval.agent_decision_id;
    SELECT * INTO snapshot FROM agent_input_snapshots WHERE snapshot_id=decision.input_snapshot_id;
    SELECT * INTO thesis FROM thesis_versions WHERE thesis_version_id=approval.thesis_version_id;
    expected_job_hash := lifecycle_json_hash(jsonb_build_object(
        'execution_intent_id', NEW.execution_intent_id,
        'entry_approval_id', NEW.entry_approval_id,
        'account_role', NEW.account_role,
        'account_fingerprint', NEW.account_fingerprint,
        'beta60', NEW.beta60::numeric(18,6),
        'benchmark_symbol', NEW.benchmark_symbol,
        'entry_boundary_at', NEW.entry_boundary_at,
        'entry_policy_hash', NEW.entry_policy_hash,
        'underlying_source_hash', NEW.underlying_source_hash,
        'benchmark_source_hash', NEW.benchmark_source_hash,
        'completed_bar_source_hash', NEW.completed_bar_source_hash,
        'prepared_at', NEW.prepared_at,
        'managed_position_id', NULL,
        'terminal_status', NULL,
        'completed_at', NULL
    ));
    IF intent.intent_id IS NULL OR approval.approval_id IS NULL OR account.role IS NULL
       OR decision.decision_id IS NULL OR snapshot.snapshot_id IS NULL
       OR thesis.thesis_version_id IS NULL
       OR intent.action<>'ENTRY' OR intent.state<>'APPROVED'
       OR intent.account_role IS DISTINCT FROM NEW.account_role
       OR intent.entry_approval_id IS DISTINCT FROM approval.approval_id
       OR intent.assessment_certificate_id IS NOT NULL
       OR intent.policy_hash IS DISTINCT FROM NEW.entry_policy_hash
       OR approval.policy_hash IS DISTINCT FROM NEW.entry_policy_hash
       OR approval.account_role IS DISTINCT FROM NEW.account_role
       OR account.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
       OR decision.thesis_version_id IS DISTINCT FROM thesis.thesis_version_id
       OR decision.input_snapshot_id IS DISTINCT FROM snapshot.snapshot_id
       OR decision.account_role IS DISTINCT FROM NEW.account_role
       OR decision.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
       OR decision.decision_kind<>'OPPORTUNITY' OR decision.outcome<>'ENTRY_APPROVED'
       OR NOT decision.autonomy_authorized
       OR snapshot.thesis_version_id IS DISTINCT FROM thesis.thesis_version_id
       OR snapshot.account_role IS DISTINCT FROM NEW.account_role
       OR snapshot.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
       OR snapshot.decision_kind<>'OPPORTUNITY'
       OR decision.decision_boundary > thesis.frozen_at
       OR NEW.entry_boundary_at > thesis.frozen_at
       OR thesis.frozen_at > snapshot.observed_at
       OR thesis.frozen_at > decision.created_at
       OR NEW.job_hash IS DISTINCT FROM expected_job_hash
    THEN RAISE EXCEPTION 'ENTRY_MATERIALIZATION_JOB_AUTHORITY_INVALID'; END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER entry_materialization_job_insert_guard
BEFORE INSERT ON entry_materialization_jobs
FOR EACH ROW EXECUTE FUNCTION entry_materialization_job_insert_guard();

CREATE FUNCTION entry_materialization_job_update_guard()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE certificate execution_certificates%ROWTYPE;
DECLARE position managed_lifecycle_positions%ROWTYPE;
BEGIN
    SELECT * INTO certificate FROM execution_certificates
    WHERE execution_intent_id=OLD.execution_intent_id;
    SELECT * INTO position FROM managed_lifecycle_positions
    WHERE managed_position_id=NEW.managed_position_id;
    IF TG_OP='DELETE' OR (to_jsonb(NEW)-ARRAY['managed_position_id','terminal_status','completed_at'])
        IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['managed_position_id','terminal_status','completed_at'])
       OR OLD.completed_at IS NOT NULL OR NEW.completed_at IS NULL
       OR certificate.certificate_id IS NULL
       OR certificate.entry_approval_id IS DISTINCT FROM OLD.entry_approval_id
       OR certificate.assessment_certificate_id IS NOT NULL
       OR certificate.execution_status IS DISTINCT FROM NEW.terminal_status
       OR (NEW.terminal_status='FILLED' AND (
            position.managed_position_id IS NULL
            OR position.entry_execution_certificate_id IS DISTINCT FROM certificate.certificate_id
            OR position.entry_intent_id IS DISTINCT FROM OLD.execution_intent_id
            OR position.entry_approval_id IS DISTINCT FROM OLD.entry_approval_id
            OR position.account_role IS DISTINCT FROM OLD.account_role
            OR position.account_fingerprint IS DISTINCT FROM OLD.account_fingerprint
       ))
    THEN RAISE EXCEPTION 'ENTRY_MATERIALIZATION_JOB_IMMUTABLE'; END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER entry_materialization_job_update_guard
BEFORE UPDATE OR DELETE ON entry_materialization_jobs
FOR EACH ROW EXECUTE FUNCTION entry_materialization_job_update_guard();

CREATE OR REPLACE FUNCTION lifecycle_agent_decision_authority_guard()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE snapshot agent_input_snapshots%ROWTYPE; thesis thesis_versions%ROWTYPE;
DECLARE tick agent_ticks%ROWTYPE;
BEGIN
    SELECT * INTO snapshot FROM agent_input_snapshots WHERE snapshot_id=NEW.input_snapshot_id;
    SELECT * INTO thesis FROM thesis_versions WHERE thesis_version_id=NEW.thesis_version_id;
    SELECT * INTO tick FROM agent_ticks WHERE tick_id=NEW.origin_tick_id;
    IF snapshot.snapshot_id IS NULL OR tick.tick_id IS NULL
       OR snapshot.thesis_version_id IS DISTINCT FROM NEW.thesis_version_id
       OR snapshot.account_role IS DISTINCT FROM NEW.account_role
       OR snapshot.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
       OR snapshot.decision_kind IS DISTINCT FROM NEW.decision_kind
       OR snapshot.decision_boundary IS DISTINCT FROM NEW.decision_boundary
       OR tick.account_role IS DISTINCT FROM NEW.account_role
       OR tick.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
       OR (NEW.thesis_version_id IS NOT NULL AND (thesis.thesis_version_id IS NULL
           OR thesis.account_role IS DISTINCT FROM NEW.account_role
           OR thesis.policy_hash IS DISTINCT FROM NEW.policy_hash
           OR (NEW.decision_kind='OPPORTUNITY'
               AND NEW.decision_boundary > thesis.frozen_at)
           OR (NEW.decision_kind='ASSESSMENT'
               AND thesis.frozen_at > NEW.decision_boundary)
           OR thesis.frozen_at > snapshot.observed_at
           OR thesis.frozen_at > NEW.created_at))
       OR (NEW.autonomy_authorized AND (NEW.thesis_version_id IS NULL OR tick.actor<>'SCHEDULER'
           OR (NEW.decision_kind='OPPORTUNITY' AND NEW.outcome<>'ENTRY_APPROVED')
           OR (NEW.decision_kind='ASSESSMENT' AND NEW.outcome NOT IN
               ('CLOSE_APPROVED','CLOSE_RISK_ONLY','ROLL_APPROVED'))))
    THEN RAISE EXCEPTION 'AGENT_DECISION_THESIS_AUTHORITY_INVALID'; END IF;
    RETURN NEW;
END
$function$;

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
                  AND certificate.execution_status = CASE
                      WHEN NEW.terminal_code = 'ENTRY_FILLED_MATERIALIZATION_FAILED'
                      THEN 'FILLED'
                      ELSE NEW.terminal_code
                  END
                  AND (
                      NEW.terminal_code <> 'ENTRY_FILLED_MATERIALIZATION_FAILED'
                      OR (certificate.entry_approval_id IS NOT NULL
                          AND certificate.entry_approval_id = intent.entry_approval_id
                          AND certificate.assessment_certificate_id IS NULL)
                  )
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
            'PARTIAL_CANCELED_RECONCILED',
            'ENTRY_FILLED_MATERIALIZATION_FAILED'
        ) THEN
            RAISE EXCEPTION 'agent tick execution terminal requires certificate';
        END IF;
        IF NEW.execution_certificate_id IS NULL AND EXISTS (
            SELECT 1 FROM execution_intents AS linked_intent
            LEFT JOIN entry_approval_certificates AS entry_authorization
              ON entry_authorization.approval_id=linked_intent.entry_approval_id
            LEFT JOIN assessment_certificates AS assessment_authorization
              ON assessment_authorization.certificate_id=linked_intent.assessment_certificate_id
            WHERE COALESCE(
                entry_authorization.agent_decision_id,
                assessment_authorization.agent_decision_id
            )=NEW.decision_id
        ) AND NEW.terminal_code NOT IN (
            'EXECUTION_BLOCKED','APPROVED_INTENT_MISMATCH',
            'ENTRY_APPROVED_WITHOUT_INTENT','ACTION_APPROVED_WITHOUT_INTENT',
            'ENTRY_MATERIALIZATION_PREPARATION_FAILED'
        ) THEN
            RAISE EXCEPTION 'agent tick certificate required for linked intent';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'agent tick transition invalid';
END
$function$;
