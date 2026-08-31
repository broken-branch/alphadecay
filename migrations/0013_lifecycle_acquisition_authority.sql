-- Migration 0012 did not retain thesis or managed-position lineage. Freeze all
-- authority tables before proving that the upgrade is a genuinely empty state.
LOCK TABLE entry_approval_certificates, assessment_certificates, execution_intents,
    order_attempts, attempt_observations, execution_certificates,
    account_reconciliation_states, whole_account_reconciliations, broker_mutation_permits,
    agent_input_snapshots, agent_decisions, agent_ticks, submission_baselines,
    competition_entry_budget, model_call_budgets, evidence_classification_claims,
    evidence_classifications IN ACCESS EXCLUSIVE MODE;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM entry_approval_certificates)
       OR EXISTS (SELECT 1 FROM assessment_certificates)
       OR EXISTS (SELECT 1 FROM execution_intents)
       OR EXISTS (SELECT 1 FROM order_attempts)
       OR EXISTS (SELECT 1 FROM attempt_observations)
       OR EXISTS (SELECT 1 FROM execution_certificates)
       OR EXISTS (SELECT 1 FROM account_reconciliation_states)
       OR EXISTS (SELECT 1 FROM whole_account_reconciliations)
       OR EXISTS (SELECT 1 FROM broker_mutation_permits)
       OR EXISTS (SELECT 1 FROM agent_input_snapshots)
       OR EXISTS (SELECT 1 FROM agent_decisions)
       OR EXISTS (SELECT 1 FROM agent_ticks)
       -- Baselines and entry budgets survive into claim-time gates. Model-call
       -- consumption and retained classifications can change lifecycle acquisition;
       -- the seeded zero-use budget is configuration, not historical authority.
       -- Performance publications and disabled recovery journals are not read by
       -- acquisition or execution authority.
       OR EXISTS (SELECT 1 FROM submission_baselines)
       OR EXISTS (SELECT 1 FROM competition_entry_budget)
       OR EXISTS (SELECT 1 FROM model_call_budgets WHERE request_count<>0)
       OR EXISTS (SELECT 1 FROM evidence_classification_claims)
       OR EXISTS (SELECT 1 FROM evidence_classifications) THEN
        RAISE EXCEPTION 'LIFECYCLE_AUTHORITY_REQUIRES_VERIFIED_ZERO_HISTORY';
    END IF;
END $$;

ALTER TABLE broker_mutation_permits
    ADD CONSTRAINT ck_lifecycle_broker_permit_time_order CHECK (
        (dispatch_acquired_at IS NULL OR issued_at <= dispatch_acquired_at)
        AND (consumed_at IS NULL OR issued_at <= consumed_at)
        AND (dispatch_acquired_at IS NULL OR consumed_at IS NULL
            OR dispatch_acquired_at <= consumed_at)
    );

CREATE FUNCTION validate_lifecycle_broker_permit_timing_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
       AND EXISTS (
           SELECT 1 FROM attempt_observations
            WHERE permit_id=NEW.permit_id
              AND observed_at<NEW.consumed_at
       )
    THEN RAISE EXCEPTION 'BROKER_PERMIT_CONSUMPTION_TIMING_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER lifecycle_broker_permit_timing_update_guard
BEFORE UPDATE ON broker_mutation_permits FOR EACH ROW
EXECUTE FUNCTION validate_lifecycle_broker_permit_timing_update();

CREATE FUNCTION validate_lifecycle_attempt_observation_timing_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE permit broker_mutation_permits%ROWTYPE;
BEGIN
    PERFORM 1 FROM order_attempts
     WHERE attempt_id=NEW.attempt_id FOR UPDATE;
    SELECT * INTO permit FROM broker_mutation_permits
     WHERE permit_id=NEW.permit_id FOR UPDATE;
    IF permit.permit_id IS NULL
       OR permit.state NOT IN ('DISPATCHING','LOOKUP_ONLY','CONSUMED')
       OR permit.dispatch_acquired_at IS NULL
       OR permit.issued_at>permit.dispatch_acquired_at
       OR permit.dispatch_acquired_at>NEW.observed_at
       OR (permit.consumed_at IS NOT NULL AND permit.consumed_at>NEW.observed_at)
       OR (permit.state='CONSUMED' AND permit.consumed_at IS NULL)
    THEN RAISE EXCEPTION 'ATTEMPT_OBSERVATION_TIMING_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER lifecycle_attempt_observation_timing_insert_guard
BEFORE INSERT ON attempt_observations FOR EACH ROW
EXECUTE FUNCTION validate_lifecycle_attempt_observation_timing_insert();

CREATE FUNCTION validate_lifecycle_reconciliation_timing_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE retrieval_started_at timestamptz; retrieval_completed_at timestamptz;
BEGIN
    retrieval_started_at:=(NEW.sweep_payload->>'retrieval_started_at')::timestamptz;
    retrieval_completed_at:=(NEW.sweep_payload->>'retrieval_completed_at')::timestamptz;
    IF retrieval_started_at IS NULL OR retrieval_completed_at IS NULL
       OR retrieval_started_at>retrieval_completed_at
       OR retrieval_completed_at>NEW.accepted_at
    THEN RAISE EXCEPTION 'RECONCILIATION_TIMING_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER lifecycle_reconciliation_timing_insert_guard
BEFORE INSERT ON whole_account_reconciliations FOR EACH ROW
EXECUTE FUNCTION validate_lifecycle_reconciliation_timing_insert();

CREATE FUNCTION validate_lifecycle_reconciliation_state_timing_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE reconciliation whole_account_reconciliations%ROWTYPE;
DECLARE permit broker_mutation_permits%ROWTYPE; observation attempt_observations%ROWTYPE;
DECLARE attempt order_attempts%ROWTYPE; observation_attempt_id uuid;
BEGIN
    IF NEW.sequence=1 THEN RETURN NEW; END IF;
    SELECT attempt_id INTO observation_attempt_id FROM attempt_observations
     WHERE observation_id=NEW.authority_observation_id;
    SELECT * INTO attempt FROM order_attempts
     WHERE attempt_id=observation_attempt_id FOR UPDATE;
    SELECT * INTO reconciliation FROM whole_account_reconciliations
     WHERE reconciliation_id=NEW.authority_reconciliation_id FOR KEY SHARE;
    SELECT * INTO permit FROM broker_mutation_permits
     WHERE permit_id=NEW.authority_permit_id FOR KEY SHARE;
    SELECT * INTO observation FROM attempt_observations
     WHERE observation_id=NEW.authority_observation_id FOR KEY SHARE;
    IF attempt.attempt_id IS NULL
       OR reconciliation.reconciliation_id IS NULL OR permit.permit_id IS NULL
       OR observation.observation_id IS NULL
       OR observation.attempt_id IS DISTINCT FROM attempt.attempt_id
       OR observation.permit_id IS DISTINCT FROM permit.permit_id
       OR permit.issued_at>permit.dispatch_acquired_at
       OR permit.dispatch_acquired_at>permit.consumed_at
       OR permit.consumed_at>observation.observed_at
       OR observation.observed_at>(reconciliation.sweep_payload->>'retrieval_started_at')::timestamptz
       OR (reconciliation.sweep_payload->>'retrieval_started_at')::timestamptz
          >(reconciliation.sweep_payload->>'retrieval_completed_at')::timestamptz
       OR (reconciliation.sweep_payload->>'retrieval_completed_at')::timestamptz
          >reconciliation.accepted_at
    THEN RAISE EXCEPTION 'RECONCILIATION_OBSERVATION_TIMING_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER lifecycle_reconciliation_state_timing_insert_guard
BEFORE INSERT ON account_reconciliation_states FOR EACH ROW
EXECUTE FUNCTION validate_lifecycle_reconciliation_state_timing_insert();

CREATE FUNCTION lifecycle_json_hash(value jsonb) RETURNS varchar(64)
IMMUTABLE STRICT LANGUAGE sql AS $$
    SELECT encode(sha256(convert_to(value::text, 'UTF8')), 'hex')
$$;

CREATE TABLE thesis_versions (
    thesis_version_id uuid PRIMARY KEY,
    thesis_id uuid NOT NULL,
    account_role varchar(16) NOT NULL REFERENCES account_roles(role),
    version integer NOT NULL CHECK (version > 0),
    thesis_hash varchar(64) NOT NULL UNIQUE CHECK (thesis_hash ~ '^[0-9a-f]{64}$'),
    policy_hash varchar(64) NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    underlying varchar(6) NOT NULL CHECK (underlying ~ '^[A-Z]{1,6}$'),
    thesis_code varchar(64) NOT NULL CHECK (thesis_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
    frozen_at timestamptz NOT NULL,
    target_at timestamptz NOT NULL CHECK (target_at > frozen_at),
    intended_exposure jsonb NOT NULL CHECK (jsonb_typeof(intended_exposure)='object' AND octet_length(intended_exposure::text)<=4096),
    exposure_limits jsonb NOT NULL CHECK (jsonb_typeof(exposure_limits)='object' AND octet_length(exposure_limits::text)<=4096),
    volatility_view varchar(16) NOT NULL CHECK (volatility_view IN ('LONG','SHORT','NEUTRAL')),
    entry_atm_iv numeric(18,8) NOT NULL CHECK (entry_atm_iv>0 AND entry_atm_iv<=100),
    approved_max_loss numeric(18,6) NOT NULL CHECK (approved_max_loss>0 AND approved_max_loss<=100000),
    portfolio_risk_cap numeric(18,6) NOT NULL CHECK (portfolio_risk_cap>0 AND portfolio_risk_cap<=100000),
    invalidation_codes jsonb NOT NULL CHECK (jsonb_typeof(invalidation_codes)='array' AND jsonb_array_length(invalidation_codes) BETWEEN 1 AND 32 AND octet_length(invalidation_codes::text)<=4096),
    thesis_payload jsonb NOT NULL CHECK (jsonb_typeof(thesis_payload)='object' AND octet_length(thesis_payload::text)<=32768),
    created_at timestamptz NOT NULL,
    UNIQUE(account_role,version), UNIQUE(thesis_id,version)
);

ALTER TABLE agent_input_snapshots ADD COLUMN thesis_version_id uuid REFERENCES thesis_versions(thesis_version_id);
ALTER TABLE agent_decisions ADD COLUMN thesis_version_id uuid REFERENCES thesis_versions(thesis_version_id);

ALTER TABLE entry_approval_certificates ADD COLUMN thesis_version_id uuid NOT NULL REFERENCES thesis_versions(thesis_version_id);
ALTER TABLE assessment_certificates ADD COLUMN thesis_version_id uuid NOT NULL REFERENCES thesis_versions(thesis_version_id);

CREATE FUNCTION lifecycle_agent_decision_authority_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
           OR thesis.policy_hash IS DISTINCT FROM NEW.policy_hash))
       OR (NEW.autonomy_authorized AND (NEW.thesis_version_id IS NULL OR tick.actor<>'SCHEDULER'
           OR (NEW.decision_kind='OPPORTUNITY' AND NEW.outcome<>'ENTRY_APPROVED')
           OR (NEW.decision_kind='ASSESSMENT' AND NEW.outcome NOT IN
               ('CLOSE_APPROVED','CLOSE_RISK_ONLY','ROLL_APPROVED'))))
    THEN RAISE EXCEPTION 'AGENT_DECISION_THESIS_AUTHORITY_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER lifecycle_agent_decision_authority_guard
AFTER INSERT ON agent_decisions DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION lifecycle_agent_decision_authority_guard();

CREATE FUNCTION lifecycle_certificate_agent_authority_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE decision agent_decisions%ROWTYPE; snapshot agent_input_snapshots%ROWTYPE;
BEGIN
    IF NEW.agent_decision_id IS NULL THEN RETURN NEW; END IF;
    SELECT * INTO decision FROM agent_decisions WHERE decision_id=NEW.agent_decision_id;
    SELECT * INTO snapshot FROM agent_input_snapshots WHERE snapshot_id=decision.input_snapshot_id;
    IF decision.decision_id IS NULL OR snapshot.snapshot_id IS NULL
       OR decision.autonomy_authorized IS NOT TRUE
       OR decision.thesis_version_id IS DISTINCT FROM NEW.thesis_version_id
       OR snapshot.thesis_version_id IS DISTINCT FROM NEW.thesis_version_id
       OR decision.account_role IS DISTINCT FROM NEW.account_role
       OR snapshot.account_role IS DISTINCT FROM NEW.account_role
       OR decision.policy_hash IS DISTINCT FROM NEW.policy_hash
    THEN RAISE EXCEPTION 'CERTIFICATE_AGENT_THESIS_AUTHORITY_INVALID'; END IF;
    IF TG_TABLE_NAME='entry_approval_certificates'
       AND (decision.decision_kind<>'OPPORTUNITY' OR decision.outcome<>'ENTRY_APPROVED')
    THEN RAISE EXCEPTION 'ENTRY_CERTIFICATE_AGENT_AUTHORITY_INVALID'; END IF;
    IF TG_TABLE_NAME='assessment_certificates'
       AND (decision.decision_kind<>'ASSESSMENT'
         OR (to_jsonb(NEW)->>'action'='ROLL' AND decision.outcome<>'ROLL_APPROVED')
         OR (to_jsonb(NEW)->>'action'='CLOSE'
             AND decision.outcome NOT IN ('CLOSE_APPROVED','CLOSE_RISK_ONLY')))
    THEN RAISE EXCEPTION 'ASSESSMENT_CERTIFICATE_AGENT_AUTHORITY_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER lifecycle_entry_certificate_agent_authority_guard
AFTER INSERT ON entry_approval_certificates DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION lifecycle_certificate_agent_authority_guard();
CREATE CONSTRAINT TRIGGER lifecycle_assessment_certificate_agent_authority_guard
AFTER INSERT ON assessment_certificates DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION lifecycle_certificate_agent_authority_guard();

CREATE TABLE greek_authority_versions (
    authority_id uuid PRIMARY KEY,
    version integer NOT NULL UNIQUE CHECK (version>0),
    effective_at timestamptz NOT NULL,
    timestamp_contract_hash varchar(64) NOT NULL UNIQUE CHECK (timestamp_contract_hash ~ '^[0-9a-f]{64}$'),
    units_contract_hash varchar(64) NOT NULL UNIQUE CHECK (units_contract_hash ~ '^[0-9a-f]{64}$'),
    authority_payload jsonb NOT NULL CHECK (jsonb_typeof(authority_payload)='object' AND octet_length(authority_payload::text)<=32768),
    authority_hash varchar(64) NOT NULL UNIQUE CHECK (authority_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL
);

CREATE TABLE alpaca_market_sessions (
    market_session_id uuid PRIMARY KEY,
    session_date date NOT NULL UNIQUE,
    open_at timestamptz NOT NULL,
    close_at timestamptz NOT NULL CHECK (close_at>open_at),
    source_hash varchar(64) NOT NULL UNIQUE CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    session_hash varchar(64) NOT NULL UNIQUE CHECK (session_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    CHECK (session_date=(open_at AT TIME ZONE 'America/New_York')::date)
);

CREATE TABLE managed_lifecycle_positions (
    managed_position_id uuid PRIMARY KEY,
    account_role varchar(16) NOT NULL REFERENCES account_roles(role),
    account_fingerprint varchar(64) NOT NULL CHECK (account_fingerprint ~ '^[0-9a-f]{64}$'),
    entry_execution_certificate_id uuid NOT NULL UNIQUE REFERENCES execution_certificates(certificate_id),
    entry_intent_id uuid NOT NULL UNIQUE REFERENCES execution_intents(intent_id),
    entry_approval_id uuid NOT NULL UNIQUE REFERENCES entry_approval_certificates(approval_id),
    thesis_version_id uuid NOT NULL REFERENCES thesis_versions(thesis_version_id),
    entry_reconciliation_id uuid NOT NULL UNIQUE REFERENCES whole_account_reconciliations(reconciliation_id),
    current_reconciliation_state_id uuid NOT NULL REFERENCES account_reconciliation_states(state_id),
    current_snapshot_id uuid,
    active_position_fingerprint varchar(64) NOT NULL CHECK (active_position_fingerprint ~ '^[0-9a-f]{64}$'),
    activated_at timestamptz NOT NULL,
    closed_at timestamptz CHECK (closed_at IS NULL OR closed_at>=activated_at)
);
CREATE UNIQUE INDEX uq_active_managed_position_role ON managed_lifecycle_positions(account_role) WHERE closed_at IS NULL;

CREATE TABLE managed_position_transitions (
    transition_id uuid PRIMARY KEY,
    managed_position_id uuid NOT NULL REFERENCES managed_lifecycle_positions(managed_position_id),
    predecessor_transition_id uuid UNIQUE REFERENCES managed_position_transitions(transition_id),
    transition_sequence integer NOT NULL CHECK (transition_sequence>=0),
    action varchar(8) NOT NULL CHECK (action IN ('ENTRY','ROLL','CLOSE')),
    execution_intent_id uuid NOT NULL UNIQUE REFERENCES execution_intents(intent_id),
    execution_certificate_id uuid NOT NULL UNIQUE REFERENCES execution_certificates(certificate_id),
    post_reconciliation_id uuid NOT NULL UNIQUE REFERENCES whole_account_reconciliations(reconciliation_id),
    fill_activity_manifest jsonb NOT NULL CHECK (jsonb_typeof(fill_activity_manifest)='array' AND jsonb_array_length(fill_activity_manifest)<=64 AND octet_length(fill_activity_manifest::text)<=65536),
    fill_activity_manifest_hash varchar(64) NOT NULL UNIQUE CHECK (fill_activity_manifest_hash ~ '^[0-9a-f]{64}$'),
    cashflow_contribution numeric(18,6) NOT NULL CHECK (abs(cashflow_contribution)<=1000000000),
    resulting_position_fingerprint varchar(64) NOT NULL CHECK (resulting_position_fingerprint ~ '^[0-9a-f]{64}$'),
    occurred_at timestamptz NOT NULL,
    market_session_id uuid NOT NULL REFERENCES alpaca_market_sessions(market_session_id),
    transition_hash varchar(64) NOT NULL UNIQUE CHECK (transition_hash ~ '^[0-9a-f]{64}$'),
    UNIQUE(managed_position_id,transition_sequence),
    CHECK ((transition_sequence=0 AND action='ENTRY' AND predecessor_transition_id IS NULL) OR (transition_sequence>0 AND predecessor_transition_id IS NOT NULL))
);

CREATE TABLE managed_position_snapshots (
    snapshot_id uuid PRIMARY KEY,
    managed_position_id uuid NOT NULL REFERENCES managed_lifecycle_positions(managed_position_id),
    predecessor_snapshot_id uuid UNIQUE REFERENCES managed_position_snapshots(snapshot_id),
    transition_id uuid NOT NULL UNIQUE REFERENCES managed_position_transitions(transition_id),
    reconciliation_id uuid NOT NULL UNIQUE REFERENCES whole_account_reconciliations(reconciliation_id),
    reconciliation_state_id uuid NOT NULL REFERENCES account_reconciliation_states(state_id),
    normalized_inventory jsonb NOT NULL CHECK (jsonb_typeof(normalized_inventory)='array' AND jsonb_array_length(normalized_inventory)<=64 AND octet_length(normalized_inventory::text)<=65536),
    inventory_hash varchar(64) NOT NULL CHECK (inventory_hash ~ '^[0-9a-f]{64}$'),
    activity_manifest jsonb NOT NULL CHECK (jsonb_typeof(activity_manifest)='array' AND jsonb_array_length(activity_manifest)<=4096 AND octet_length(activity_manifest::text)<=1048576),
    activity_manifest_hash varchar(64) NOT NULL CHECK (activity_manifest_hash ~ '^[0-9a-f]{64}$'),
    cumulative_cashflow numeric(18,6) NOT NULL CHECK (abs(cumulative_cashflow)<=1000000000),
    rolls_on_trading_day integer NOT NULL CHECK (rolls_on_trading_day BETWEEN 0 AND 64),
    market_session_id uuid NOT NULL REFERENCES alpaca_market_sessions(market_session_id),
    position_fingerprint varchar(64) NOT NULL CHECK (position_fingerprint ~ '^[0-9a-f]{64}$'),
    accepted_at timestamptz NOT NULL,
    snapshot_hash varchar(64) NOT NULL UNIQUE CHECK (snapshot_hash ~ '^[0-9a-f]{64}$')
);
ALTER TABLE managed_lifecycle_positions ADD CONSTRAINT fk_managed_current_snapshot FOREIGN KEY(current_snapshot_id) REFERENCES managed_position_snapshots(snapshot_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE lifecycle_observation_manifests (
    manifest_id uuid PRIMARY KEY,
    manifest_hash varchar(64) NOT NULL UNIQUE CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
    agent_input_snapshot_id uuid NOT NULL UNIQUE REFERENCES agent_input_snapshots(snapshot_id),
    managed_position_id uuid NOT NULL REFERENCES managed_lifecycle_positions(managed_position_id),
    managed_snapshot_id uuid NOT NULL REFERENCES managed_position_snapshots(snapshot_id),
    reconciliation_id uuid NOT NULL REFERENCES whole_account_reconciliations(reconciliation_id),
    greek_authority_id uuid NOT NULL REFERENCES greek_authority_versions(authority_id),
    sweep_hash varchar(64) NOT NULL CHECK (sweep_hash ~ '^[0-9a-f]{64}$'),
    account_manifest jsonb NOT NULL CHECK (jsonb_typeof(account_manifest)='object'),
    activity_manifest jsonb NOT NULL CHECK (jsonb_typeof(activity_manifest)='array'),
    option_manifest jsonb NOT NULL CHECK (jsonb_typeof(option_manifest)='array'),
    atm_iv_manifest jsonb NOT NULL CHECK (jsonb_typeof(atm_iv_manifest)='object'),
    underlying_manifest jsonb NOT NULL CHECK (jsonb_typeof(underlying_manifest)='object'),
    boundary_manifest jsonb NOT NULL CHECK (jsonb_typeof(boundary_manifest)='object'),
    research_manifest jsonb NOT NULL CHECK (jsonb_typeof(research_manifest)='array'),
    observed_at timestamptz NOT NULL, created_at timestamptz NOT NULL CHECK (created_at>=observed_at),
    CHECK (octet_length(account_manifest::text)+octet_length(activity_manifest::text)+octet_length(option_manifest::text)+octet_length(atm_iv_manifest::text)+octet_length(underlying_manifest::text)+octet_length(boundary_manifest::text)+octet_length(research_manifest::text)<=1048576)
);

CREATE FUNCTION lifecycle_derive_thesis_hash() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.thesis_hash := lifecycle_json_hash(to_jsonb(NEW)-ARRAY['thesis_hash','created_at']);
    RETURN NEW;
END $$;
CREATE TRIGGER thesis_versions_derive_hash BEFORE INSERT ON thesis_versions FOR EACH ROW EXECUTE FUNCTION lifecycle_derive_thesis_hash();

CREATE FUNCTION lifecycle_derive_greek_hash() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.authority_hash := lifecycle_json_hash(to_jsonb(NEW)-ARRAY['authority_hash','created_at']);
    RETURN NEW;
END $$;
CREATE TRIGGER greek_authority_derive_hash BEFORE INSERT ON greek_authority_versions FOR EACH ROW EXECUTE FUNCTION lifecycle_derive_greek_hash();

CREATE FUNCTION alpaca_market_session_unavailable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ALPACA_MARKET_SESSION_PROVIDER_AUTHORITY_UNAVAILABLE';
END $$;
CREATE TRIGGER alpaca_market_session_unavailable_guard BEFORE INSERT ON alpaca_market_sessions
FOR EACH ROW EXECUTE FUNCTION alpaca_market_session_unavailable_guard();

CREATE FUNCTION lifecycle_authority_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'LIFECYCLE_AUTHORITY_APPEND_ONLY'; END $$;
CREATE TRIGGER thesis_versions_append_only BEFORE UPDATE OR DELETE ON thesis_versions FOR EACH ROW EXECUTE FUNCTION lifecycle_authority_append_only();
CREATE TRIGGER greek_authority_versions_append_only BEFORE UPDATE OR DELETE ON greek_authority_versions FOR EACH ROW EXECUTE FUNCTION lifecycle_authority_append_only();
CREATE TRIGGER alpaca_market_sessions_append_only BEFORE UPDATE OR DELETE ON alpaca_market_sessions FOR EACH ROW EXECUTE FUNCTION lifecycle_authority_append_only();
CREATE TRIGGER managed_position_transitions_append_only BEFORE UPDATE OR DELETE ON managed_position_transitions FOR EACH ROW EXECUTE FUNCTION lifecycle_authority_append_only();
CREATE TRIGGER managed_position_snapshots_append_only BEFORE UPDATE OR DELETE ON managed_position_snapshots FOR EACH ROW EXECUTE FUNCTION lifecycle_authority_append_only();
CREATE TRIGGER lifecycle_observation_manifest_append_only BEFORE UPDATE OR DELETE ON lifecycle_observation_manifests FOR EACH ROW EXECUTE FUNCTION lifecycle_authority_append_only();

CREATE FUNCTION managed_position_transition_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    intent execution_intents%ROWTYPE; certificate execution_certificates%ROWTYPE;
    position managed_lifecycle_positions%ROWTYPE; reconciliation whole_account_reconciliations%ROWTYPE;
    approval entry_approval_certificates%ROWTYPE; assessment assessment_certificates%ROWTYPE;
    predecessor managed_position_transitions%ROWTYPE; predecessor_snapshot managed_position_snapshots%ROWTYPE;
    successor_snapshot managed_position_snapshots%ROWTYPE; market_session alpaca_market_sessions%ROWTYPE; prior_activities jsonb := '[]'::jsonb;
    derived_activities jsonb; derived_cashflow numeric(18,6); derived_position_hash varchar(64);
BEGIN
    SELECT * INTO intent FROM execution_intents WHERE intent_id=NEW.execution_intent_id;
    SELECT * INTO certificate FROM execution_certificates WHERE certificate_id=NEW.execution_certificate_id;
    SELECT * INTO position FROM managed_lifecycle_positions WHERE managed_position_id=NEW.managed_position_id;
    SELECT * INTO reconciliation FROM whole_account_reconciliations WHERE reconciliation_id=NEW.post_reconciliation_id;
    SELECT * INTO market_session FROM alpaca_market_sessions WHERE market_session_id=NEW.market_session_id;
    SELECT * INTO approval FROM entry_approval_certificates WHERE approval_id=position.entry_approval_id;
    SELECT * INTO assessment FROM assessment_certificates WHERE certificate_id=intent.assessment_certificate_id;
    SELECT COALESCE(sum(filled_cash_flow),0) INTO derived_cashflow FROM order_attempts
      WHERE execution_intent_id=intent.intent_id AND filled_quantity>0;
    IF NEW.transition_sequence>0 THEN
        SELECT * INTO predecessor FROM managed_position_transitions WHERE transition_id=NEW.predecessor_transition_id;
        SELECT * INTO predecessor_snapshot FROM managed_position_snapshots WHERE transition_id=predecessor.transition_id;
        SELECT * INTO successor_snapshot FROM managed_position_snapshots WHERE transition_id=NEW.transition_id;
        prior_activities := predecessor_snapshot.activity_manifest;
    END IF;
    SELECT COALESCE(jsonb_agg(activity ORDER BY activity->>'activity_id_hash'),'[]'::jsonb)
      INTO derived_activities FROM jsonb_array_elements(COALESCE(reconciliation.sweep_payload->'activities','[]'::jsonb)) activity
     WHERE activity->>'activity_type' IN ('OPTRD','FILL')
       AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(prior_activities) prior WHERE prior->>'activity_id_hash'=activity->>'activity_id_hash');
    derived_position_hash := lifecycle_json_hash(COALESCE(reconciliation.sweep_payload->'final_positions','[]'::jsonb));
    IF intent.intent_id IS NULL OR certificate.certificate_id IS NULL OR position.managed_position_id IS NULL OR reconciliation.reconciliation_id IS NULL
       OR intent.state<>'TERMINAL' OR intent.action IS DISTINCT FROM NEW.action OR intent.account_role IS DISTINCT FROM position.account_role
       OR intent.policy_hash IS DISTINCT FROM (SELECT policy_hash FROM thesis_versions WHERE thesis_version_id=position.thesis_version_id)
       OR certificate.execution_intent_id IS DISTINCT FROM intent.intent_id OR certificate.execution_status<>'FILLED'
       OR certificate.reconciliation_id IS DISTINCT FROM reconciliation.reconciliation_id OR reconciliation.safe IS NOT TRUE
       OR market_session.market_session_id IS NULL OR NEW.occurred_at<market_session.open_at OR NEW.occurred_at>market_session.close_at
       OR reconciliation.account_role IS DISTINCT FROM position.account_role OR reconciliation.account_fingerprint IS DISTINCT FROM position.account_fingerprint
       OR reconciliation.execution_intent_id IS DISTINCT FROM intent.intent_id OR NEW.cashflow_contribution IS DISTINCT FROM derived_cashflow
       OR EXISTS (SELECT 1 FROM order_attempts attempt WHERE attempt.execution_intent_id=intent.intent_id
            AND attempt.filled_quantity>0 AND NOT (certificate.attempt_ids ? attempt.attempt_id::text))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(certificate.attempt_ids) listed(attempt_id)
            WHERE NOT EXISTS (SELECT 1 FROM order_attempts attempt WHERE attempt.attempt_id::text=listed.attempt_id
                AND attempt.execution_intent_id=intent.intent_id))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(derived_activities) activity
            WHERE 1<>(SELECT count(*) FROM order_attempts attempt WHERE attempt.execution_intent_id=intent.intent_id
                AND attempt.filled_quantity>0
                AND NULLIF(attempt.client_order_id,'') IS NOT NULL
                AND (NULLIF(attempt.provider_order_id,'') IS NULL OR attempt.provider_order_id=activity->>'provider_order_id')
                AND attempt.client_order_id=activity->>'client_order_id'))
       OR EXISTS (SELECT 1 FROM order_attempts attempt WHERE attempt.execution_intent_id=intent.intent_id
            AND attempt.filled_quantity>0 AND (NULLIF(attempt.client_order_id,'') IS NULL
              OR 1<>(SELECT count(*) FROM jsonb_array_elements(derived_activities) activity
                WHERE (NULLIF(attempt.provider_order_id,'') IS NULL OR attempt.provider_order_id=activity->>'provider_order_id')
                  AND attempt.client_order_id=activity->>'client_order_id')))
       OR NEW.fill_activity_manifest IS DISTINCT FROM derived_activities OR NEW.fill_activity_manifest_hash IS DISTINCT FROM lifecycle_json_hash(derived_activities)
       OR NEW.resulting_position_fingerprint IS DISTINCT FROM derived_position_hash OR NEW.occurred_at IS DISTINCT FROM reconciliation.accepted_at
       OR NEW.transition_hash IS DISTINCT FROM lifecycle_json_hash(to_jsonb(NEW)-'transition_hash')
    THEN RAISE EXCEPTION 'MANAGED_POSITION_TRANSITION_LINEAGE_INVALID'; END IF;
    IF NEW.transition_sequence=0 AND (NEW.execution_intent_id IS DISTINCT FROM position.entry_intent_id
       OR NEW.execution_certificate_id IS DISTINCT FROM position.entry_execution_certificate_id
       OR NEW.post_reconciliation_id IS DISTINCT FROM position.entry_reconciliation_id
       OR intent.entry_approval_id IS DISTINCT FROM position.entry_approval_id
       OR approval.thesis_version_id IS DISTINCT FROM position.thesis_version_id)
    THEN RAISE EXCEPTION 'MANAGED_POSITION_ENTRY_TRANSITION_INVALID'; END IF;
    IF NEW.transition_sequence>0 AND (predecessor.managed_position_id IS DISTINCT FROM NEW.managed_position_id
       OR predecessor.transition_sequence<>NEW.transition_sequence-1 OR NEW.occurred_at<predecessor.occurred_at
       OR assessment.certificate_id IS NULL OR assessment.account_role IS DISTINCT FROM position.account_role
       OR assessment.position_fingerprint IS DISTINCT FROM predecessor_snapshot.position_fingerprint
       OR assessment.created_at<market_session.open_at OR assessment.created_at>market_session.close_at
       OR assessment.policy_hash IS DISTINCT FROM intent.policy_hash OR assessment.thesis_version_id IS DISTINCT FROM position.thesis_version_id
       OR certificate.assessment_certificate_id IS DISTINCT FROM assessment.certificate_id
       OR successor_snapshot.snapshot_id IS NULL OR successor_snapshot.predecessor_snapshot_id IS DISTINCT FROM predecessor_snapshot.snapshot_id
       OR position.current_snapshot_id IS DISTINCT FROM successor_snapshot.snapshot_id
       OR position.current_reconciliation_state_id IS DISTINCT FROM successor_snapshot.reconciliation_state_id
       OR position.active_position_fingerprint IS DISTINCT FROM successor_snapshot.position_fingerprint
       OR (NEW.action='ROLL' AND position.closed_at IS NOT NULL)
       OR (NEW.action='CLOSE' AND position.closed_at IS DISTINCT FROM NEW.occurred_at))
    THEN RAISE EXCEPTION 'MANAGED_POSITION_TRANSITION_PREDECESSOR_INVALID'; END IF;
    IF (NEW.action='CLOSE') IS DISTINCT FROM (jsonb_array_length(COALESCE(reconciliation.sweep_payload->'final_positions','[]'::jsonb))=0)
    THEN RAISE EXCEPTION 'MANAGED_POSITION_TERMINAL_INVENTORY_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER managed_position_transition_guard AFTER INSERT ON managed_position_transitions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION managed_position_transition_guard();

CREATE FUNCTION managed_position_snapshot_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE transition_record managed_position_transitions%ROWTYPE; predecessor managed_position_snapshots%ROWTYPE;
DECLARE reconciliation whole_account_reconciliations%ROWTYPE; reconciliation_state account_reconciliation_states%ROWTYPE; expected_cashflow numeric(18,6); expected_rolls integer;
DECLARE expected_inventory jsonb; expected_activities jsonb;
BEGIN
    SELECT * INTO transition_record FROM managed_position_transitions WHERE transition_id=NEW.transition_id;
    SELECT * INTO reconciliation FROM whole_account_reconciliations WHERE reconciliation_id=NEW.reconciliation_id;
    SELECT * INTO reconciliation_state FROM account_reconciliation_states WHERE state_id=NEW.reconciliation_state_id;
    SELECT * INTO predecessor FROM managed_position_snapshots WHERE snapshot_id=NEW.predecessor_snapshot_id;
    SELECT COALESCE(sum(cashflow_contribution),0),count(*) FILTER(WHERE action='ROLL' AND market_session_id=NEW.market_session_id)
      INTO expected_cashflow,expected_rolls FROM managed_position_transitions
     WHERE managed_position_id=NEW.managed_position_id AND transition_sequence<=transition_record.transition_sequence;
    expected_inventory:=COALESCE(reconciliation.sweep_payload->'final_positions','[]'::jsonb);
    expected_activities:=COALESCE(reconciliation.sweep_payload->'activities','[]'::jsonb);
    IF transition_record.managed_position_id IS DISTINCT FROM NEW.managed_position_id
       OR transition_record.post_reconciliation_id IS DISTINCT FROM NEW.reconciliation_id
       OR NEW.market_session_id IS DISTINCT FROM transition_record.market_session_id
       OR NEW.normalized_inventory IS DISTINCT FROM expected_inventory OR NEW.inventory_hash IS DISTINCT FROM lifecycle_json_hash(expected_inventory)
       OR NEW.activity_manifest IS DISTINCT FROM expected_activities OR NEW.activity_manifest_hash IS DISTINCT FROM lifecycle_json_hash(expected_activities)
       OR NEW.cumulative_cashflow IS DISTINCT FROM expected_cashflow OR NEW.rolls_on_trading_day IS DISTINCT FROM expected_rolls
       OR NEW.position_fingerprint IS DISTINCT FROM lifecycle_json_hash(expected_inventory) OR NEW.accepted_at IS DISTINCT FROM reconciliation.accepted_at
       OR NEW.snapshot_hash IS DISTINCT FROM lifecycle_json_hash(to_jsonb(NEW)-'snapshot_hash')
       OR (transition_record.transition_sequence=0 AND NEW.predecessor_snapshot_id IS NOT NULL)
       OR (transition_record.transition_sequence>0 AND (predecessor.managed_position_id IS DISTINCT FROM NEW.managed_position_id
           OR predecessor.transition_id IS DISTINCT FROM transition_record.predecessor_transition_id OR NEW.accepted_at<predecessor.accepted_at))
       OR reconciliation_state.state_id IS NULL
       OR reconciliation_state.authority_reconciliation_id IS DISTINCT FROM reconciliation.reconciliation_id
       OR reconciliation_state.account_role IS DISTINCT FROM reconciliation.account_role
       OR reconciliation_state.account_fingerprint IS DISTINCT FROM reconciliation.account_fingerprint
       OR reconciliation_state.accepted_at IS DISTINCT FROM reconciliation.accepted_at
       OR reconciliation_state.expected_positions IS DISTINCT FROM expected_inventory
       OR reconciliation_state.expected_open_orders IS DISTINCT FROM COALESCE(reconciliation.expectation_payload->'expected_open_orders','[]'::jsonb)
       OR reconciliation_state.expected_cash IS DISTINCT FROM (reconciliation.expectation_payload->>'expected_cash')::numeric
       OR reconciliation_state.known_activities IS DISTINCT FROM expected_activities
       OR NOT EXISTS (SELECT 1 FROM broker_mutation_permits permit
            JOIN attempt_observations observation ON observation.permit_id=permit.permit_id
            JOIN order_attempts attempt ON attempt.attempt_id=observation.attempt_id
           WHERE permit.permit_id=reconciliation_state.authority_permit_id
             AND observation.observation_id=reconciliation_state.authority_observation_id
             AND permit.reconciliation_id=reconciliation.reconciliation_id
             AND permit.execution_intent_id=transition_record.execution_intent_id
             AND permit.execution_intent_id=reconciliation.execution_intent_id
             AND permit.intent_digest=reconciliation.intent_digest
             AND permit.attempt_ordinal=reconciliation.attempt_ordinal
             AND permit.mutation_kind=reconciliation.purpose
             AND permit.state='CONSUMED'
             AND reconciliation_state.authority_permit_request_hash=permit.request_hash
             AND permit.request_hash=reconciliation.request_hash
             AND permit.request_hash=reconciliation.expectation_payload->>'request_hash'
             AND observation.execution_intent_id=transition_record.execution_intent_id
             AND observation.attempt_ordinal=reconciliation.attempt_ordinal
             AND observation.attempt_ordinal=attempt.attempt_ordinal
             AND observation.provider_present
             AND permit.issued_at<=permit.dispatch_acquired_at
             AND permit.dispatch_acquired_at<=permit.consumed_at
             AND permit.consumed_at<=observation.observed_at
             AND observation.observed_at<=(reconciliation.sweep_payload->>'retrieval_started_at')::timestamptz
             AND (reconciliation.sweep_payload->>'retrieval_started_at')::timestamptz
                 <=(reconciliation.sweep_payload->>'retrieval_completed_at')::timestamptz
             AND (reconciliation.sweep_payload->>'retrieval_started_at')::timestamptz<=reconciliation.accepted_at
             AND (reconciliation.sweep_payload->>'retrieval_completed_at')::timestamptz<=reconciliation.accepted_at
             AND attempt.execution_intent_id=transition_record.execution_intent_id
             AND attempt.broker_permit_id=permit.permit_id
             AND attempt.request_hash=permit.request_hash
             AND attempt.state='FILLED'
             AND attempt.filled_quantity>0
             AND observation.observed_payload->>'intent_id'=attempt.execution_intent_id::text
             AND (observation.observed_payload->>'ordinal')::integer=attempt.attempt_ordinal
             AND observation.observed_payload->>'client_order_id'=attempt.client_order_id
             AND observation.observed_payload->>'request_hash'=attempt.request_hash
             AND observation.observed_payload->>'state'=attempt.state
             AND observation.observed_payload->>'provider_order_id'=attempt.provider_order_id
             AND (observation.observed_payload->>'filled_quantity')::integer=attempt.filled_quantity
             AND (observation.observed_payload->>'quantity')::integer=attempt.quantity
             AND (observation.observed_payload->>'fill_cash_flow')::numeric=attempt.filled_cash_flow
             AND NOT EXISTS (SELECT 1 FROM attempt_observations later
                  WHERE later.execution_intent_id=observation.execution_intent_id
                    AND later.observation_sequence>observation.observation_sequence))
       OR (transition_record.transition_sequence>0 AND (reconciliation_state.predecessor_state_id IS DISTINCT FROM predecessor.reconciliation_state_id
           OR reconciliation_state.sequence IS DISTINCT FROM (SELECT sequence+1 FROM account_reconciliation_states WHERE state_id=predecessor.reconciliation_state_id)))
    THEN RAISE EXCEPTION 'MANAGED_POSITION_SNAPSHOT_DERIVATION_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER managed_position_snapshot_guard AFTER INSERT ON managed_position_snapshots DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION managed_position_snapshot_guard();

CREATE FUNCTION managed_position_mutation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE snapshot managed_position_snapshots%ROWTYPE; transition_record managed_position_transitions%ROWTYPE;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'MANAGED_POSITION_IMMUTABLE'; END IF;
    IF (to_jsonb(NEW)-ARRAY['current_reconciliation_state_id','current_snapshot_id','active_position_fingerprint','closed_at'])
       IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['current_reconciliation_state_id','current_snapshot_id','active_position_fingerprint','closed_at'])
    THEN RAISE EXCEPTION 'MANAGED_POSITION_IMMUTABLE'; END IF;
    SELECT * INTO snapshot FROM managed_position_snapshots WHERE snapshot_id=NEW.current_snapshot_id;
    SELECT * INTO transition_record FROM managed_position_transitions WHERE transition_id=snapshot.transition_id;
    IF snapshot.managed_position_id IS DISTINCT FROM NEW.managed_position_id OR snapshot.position_fingerprint IS DISTINCT FROM NEW.active_position_fingerprint
       OR snapshot.reconciliation_state_id IS DISTINCT FROM NEW.current_reconciliation_state_id
       OR EXISTS (SELECT 1 FROM managed_position_snapshots later JOIN managed_position_transitions later_transition ON later_transition.transition_id=later.transition_id
           WHERE later.managed_position_id=NEW.managed_position_id AND (later_transition.transition_sequence>transition_record.transition_sequence
             OR (later_transition.transition_sequence=transition_record.transition_sequence AND later.snapshot_id>snapshot.snapshot_id)))
       OR (NEW.closed_at IS NOT NULL AND (OLD.closed_at IS NOT NULL OR transition_record.action<>'CLOSE'
             OR jsonb_array_length(snapshot.normalized_inventory)<>0 OR NEW.closed_at IS DISTINCT FROM transition_record.occurred_at))
       OR (NEW.closed_at IS NULL AND transition_record.action='CLOSE')
    THEN RAISE EXCEPTION 'MANAGED_POSITION_CURRENT_SNAPSHOT_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER managed_position_mutation_guard BEFORE UPDATE OR DELETE ON managed_lifecycle_positions FOR EACH ROW EXECUTE FUNCTION managed_position_mutation_guard();

CREATE FUNCTION managed_position_activation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE intent execution_intents%ROWTYPE; certificate execution_certificates%ROWTYPE; approval entry_approval_certificates%ROWTYPE;
DECLARE thesis thesis_versions%ROWTYPE; reconciliation whole_account_reconciliations%ROWTYPE; snapshot managed_position_snapshots%ROWTYPE;
BEGIN
    SELECT * INTO intent FROM execution_intents WHERE intent_id=NEW.entry_intent_id;
    SELECT * INTO certificate FROM execution_certificates WHERE certificate_id=NEW.entry_execution_certificate_id;
    SELECT * INTO approval FROM entry_approval_certificates WHERE approval_id=NEW.entry_approval_id;
    SELECT * INTO thesis FROM thesis_versions WHERE thesis_version_id=NEW.thesis_version_id;
    SELECT * INTO reconciliation FROM whole_account_reconciliations WHERE reconciliation_id=NEW.entry_reconciliation_id;
    SELECT * INTO snapshot FROM managed_position_snapshots WHERE snapshot_id=NEW.current_snapshot_id;
    IF NEW.current_snapshot_id IS NULL OR NEW.closed_at IS NOT NULL OR intent.action<>'ENTRY' OR intent.state<>'TERMINAL'
       OR intent.account_role IS DISTINCT FROM NEW.account_role OR intent.entry_approval_id IS DISTINCT FROM approval.approval_id
       OR intent.policy_hash IS DISTINCT FROM thesis.policy_hash OR certificate.execution_intent_id IS DISTINCT FROM intent.intent_id
       OR certificate.entry_approval_id IS DISTINCT FROM approval.approval_id OR certificate.execution_status<>'FILLED'
       OR certificate.reconciliation_id IS DISTINCT FROM reconciliation.reconciliation_id OR approval.account_role IS DISTINCT FROM NEW.account_role
       OR approval.policy_hash IS DISTINCT FROM thesis.policy_hash OR approval.thesis_version_id IS DISTINCT FROM thesis.thesis_version_id
       OR thesis.account_role IS DISTINCT FROM NEW.account_role OR reconciliation.safe IS NOT TRUE
       OR reconciliation.account_role IS DISTINCT FROM NEW.account_role OR reconciliation.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
       OR reconciliation.execution_intent_id IS DISTINCT FROM intent.intent_id OR snapshot.managed_position_id IS DISTINCT FROM NEW.managed_position_id
       OR snapshot.reconciliation_id IS DISTINCT FROM NEW.entry_reconciliation_id OR snapshot.position_fingerprint IS DISTINCT FROM NEW.active_position_fingerprint
       OR EXISTS (SELECT 1 FROM managed_position_snapshots later
            JOIN managed_position_transitions later_transition ON later_transition.transition_id=later.transition_id
            JOIN managed_position_transitions current_transition ON current_transition.transition_id=snapshot.transition_id
           WHERE later.managed_position_id=NEW.managed_position_id
             AND (later_transition.transition_sequence>current_transition.transition_sequence
               OR (later_transition.transition_sequence=current_transition.transition_sequence AND later.snapshot_id>snapshot.snapshot_id)))
       OR NEW.activated_at IS DISTINCT FROM reconciliation.accepted_at
    THEN RAISE EXCEPTION 'MANAGED_POSITION_ACTIVATION_LINEAGE_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER managed_position_activation_guard AFTER INSERT ON managed_lifecycle_positions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION managed_position_activation_guard();

CREATE FUNCTION lifecycle_manifest_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- No immutable provider/calendar/research ingest rows exist yet. A caller hash
    -- cannot promote JSON into evidence authority, even when all rows are inserted
    -- in one transaction. Keep persistence unavailable until those adapters land.
    RAISE EXCEPTION 'LIFECYCLE_OBSERVATION_PROVIDER_AUTHORITY_UNAVAILABLE';
END $$;
CREATE CONSTRAINT TRIGGER lifecycle_manifest_guard AFTER INSERT ON lifecycle_observation_manifests DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lifecycle_manifest_guard();
