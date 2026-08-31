DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM development_opportunity_plans) THEN
        RAISE EXCEPTION 'OPPORTUNITY_PLAN_AUTHORITY_MIGRATION_REQUIRES_ZERO_PLANS';
    END IF;
END $$;

ALTER TABLE development_opportunity_plans
    ADD COLUMN daily_start_session date NOT NULL,
    ADD COLUMN allowed_event_codes jsonb NOT NULL
        CHECK (jsonb_typeof(allowed_event_codes) = 'array'
               AND jsonb_array_length(allowed_event_codes) BETWEEN 1 AND 12),
    ADD COLUMN evidence_window_start timestamptz NOT NULL,
    ADD COLUMN evidence_window_end timestamptz NOT NULL,
    ADD CONSTRAINT ck_opportunity_plan_acquisition_sessions
        CHECK (daily_start_session < pre_event_session),
    ADD CONSTRAINT ck_opportunity_plan_acquisition_window
        CHECK (frozen_at <= evidence_window_start
               AND evidence_window_start <= evidence_window_end);

CREATE OR REPLACE FUNCTION development_opportunity_plan_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    prior development_opportunity_plans%ROWTYPE;
BEGIN
    PERFORM 1 FROM account_roles WHERE role = 'DEVELOPMENT' FOR UPDATE;
    SELECT * INTO prior FROM development_opportunity_plans
     WHERE opportunity_key = NEW.opportunity_key
     ORDER BY version DESC LIMIT 1;
    IF NEW.account_role <> 'DEVELOPMENT'
       OR NEW.benchmark_symbol <> 'QQQ'
       OR NEW.daily_start_session >= NEW.pre_event_session
       OR extract(isodow FROM NEW.daily_start_session) > 5
       OR NEW.signal_session - NEW.daily_start_session NOT BETWEEN 1 AND 120
       OR jsonb_typeof(NEW.allowed_event_codes) <> 'array'
       OR jsonb_array_length(NEW.allowed_event_codes) NOT BETWEEN 1 AND 12
       OR EXISTS (
           SELECT 1 FROM jsonb_array_elements(NEW.allowed_event_codes) item
            WHERE jsonb_typeof(item) <> 'string'
               OR item #>> '{}' !~ '^[A-Z][A-Z0-9_]{0,63}$'
       )
       OR (
           SELECT count(*) <> count(DISTINCT item #>> '{}')
             FROM jsonb_array_elements(NEW.allowed_event_codes) item
       )
       OR NOT (NEW.frozen_at <= NEW.evidence_window_start
               AND NEW.evidence_window_start <= NEW.evidence_window_end)
       OR jsonb_typeof(NEW.policy_payload) <> 'object'
       OR jsonb_typeof(NEW.request_contract) <> 'object'
       OR jsonb_typeof(NEW.thesis_target_contract) <> 'object'
       OR jsonb_typeof(NEW.exposure_limit_contract) <> 'object'
       OR EXISTS (
           SELECT 1 FROM jsonb_array_elements(NEW.invalidation_codes) item
            WHERE jsonb_typeof(item) <> 'string'
               OR item #>> '{}' !~ '^[A-Z][A-Z0-9_]{0,63}$'
       )
       OR (
           SELECT count(*) <> count(DISTINCT item #>> '{}')
             FROM jsonb_array_elements(NEW.invalidation_codes) item
       )
       OR NEW.policy_hash IS DISTINCT FROM opportunity_evidence_hash(
              'alphadecay.opportunity.policy.v1', NEW.policy_payload)
       OR NEW.policy_payload->>'opportunity_key' IS DISTINCT FROM NEW.opportunity_key
       OR NEW.policy_payload->>'underlying' IS DISTINCT FROM NEW.underlying
       OR NEW.policy_payload->>'selected_decision_boundary' !~ 'Z$'
       OR (NEW.policy_payload->>'selected_decision_boundary')::timestamptz::date
              IS DISTINCT FROM NEW.signal_session
       OR NEW.evidence_window_end >
              (NEW.policy_payload->>'selected_decision_boundary')::timestamptz
       OR NEW.request_contract_hash IS DISTINCT FROM opportunity_plain_hash(
              NEW.request_contract)
       OR NEW.request_contract->>'account_fingerprint' IS DISTINCT FROM (
              SELECT account_fingerprint FROM account_roles WHERE role = 'DEVELOPMENT')
       OR NEW.request_contract->>'underlying' IS DISTINCT FROM NEW.underlying
       OR NEW.request_contract->>'benchmark' IS DISTINCT FROM NEW.benchmark_symbol
       OR NEW.request_contract->>'decision_boundary' !~ '\+00:00$'
       OR (NEW.request_contract->>'decision_boundary')::timestamptz IS DISTINCT FROM
              (NEW.policy_payload->>'selected_decision_boundary')::timestamptz
       OR (SELECT count(*) FROM jsonb_object_keys(NEW.request_contract)) <> 11
       OR NOT (NEW.request_contract ?& ARRAY[
              'account_fingerprint', 'underlying', 'benchmark', 'decision_boundary',
              'minimum_expiry', 'maximum_expiry', 'minimum_strike', 'maximum_strike',
              'maximum_contracts', 'maximum_quote_age_seconds',
              'maximum_quote_skew_seconds'])
       OR jsonb_typeof(NEW.request_contract->'maximum_contracts') <> 'number'
       OR jsonb_typeof(NEW.request_contract->'maximum_quote_age_seconds') <> 'number'
       OR jsonb_typeof(NEW.request_contract->'maximum_quote_skew_seconds') <> 'number'
       OR extract(second FROM
              (NEW.request_contract->>'decision_boundary')::timestamptz) <> 0
       OR extract(minute FROM
              (NEW.request_contract->>'decision_boundary')::timestamptz)::integer % 5 <> 0
       OR (NEW.request_contract->>'decision_boundary')::timestamptz::date >
              (NEW.request_contract->>'minimum_expiry')::date
       OR (NEW.request_contract->>'minimum_expiry')::date >
              (NEW.request_contract->>'maximum_expiry')::date
       OR (NEW.request_contract->>'maximum_expiry')::date -
              (NEW.request_contract->>'minimum_expiry')::date > 45
       OR (NEW.request_contract->>'minimum_strike')::numeric <= 0
       OR (NEW.request_contract->>'maximum_strike')::numeric <
              (NEW.request_contract->>'minimum_strike')::numeric
       OR (NEW.request_contract->>'maximum_strike')::numeric -
              (NEW.request_contract->>'minimum_strike')::numeric > 1000
       OR (NEW.request_contract->>'maximum_contracts')::integer NOT BETWEEN 1 AND 128
       OR (NEW.request_contract->>'maximum_contracts')::numeric <>
              trunc((NEW.request_contract->>'maximum_contracts')::numeric)
       OR (NEW.request_contract->>'maximum_quote_age_seconds')::numeric <= 0
       OR (NEW.request_contract->>'maximum_quote_age_seconds')::numeric > 120
       OR (NEW.request_contract->>'maximum_quote_skew_seconds')::numeric <= 0
       OR (NEW.request_contract->>'maximum_quote_skew_seconds')::numeric > 30
       OR NEW.thesis_target_hash IS DISTINCT FROM opportunity_evidence_hash(
              'alphadecay.opportunity.thesis-target.v1', NEW.thesis_target_contract)
       OR NEW.exposure_limit_hash IS DISTINCT FROM opportunity_evidence_hash(
              'alphadecay.opportunity.exposure-limit.v1', NEW.exposure_limit_contract)
       OR NEW.plan_hash IS DISTINCT FROM opportunity_evidence_hash(
              'alphadecay.opportunity.plan.v1', NEW.plan_material)
       OR NEW.plan_material IS DISTINCT FROM jsonb_build_object(
              'account_role', NEW.account_role,
              'opportunity_key', NEW.opportunity_key,
              'version', NEW.version,
              'underlying', NEW.underlying,
              'benchmark_symbol', NEW.benchmark_symbol,
              'event_session', NEW.event_session::text,
              'pre_event_session', NEW.pre_event_session::text,
              'reaction_session', NEW.reaction_session::text,
              'signal_session', NEW.signal_session::text,
              'daily_start_session', NEW.daily_start_session::text,
              'allowed_event_codes', NEW.allowed_event_codes,
              'evidence_window_start', NEW.plan_material->>'evidence_window_start',
              'evidence_window_end', NEW.plan_material->>'evidence_window_end',
              'policy_payload', NEW.policy_payload,
              'policy_hash', NEW.policy_hash,
              'request_contract', NEW.request_contract,
              'request_contract_hash', NEW.request_contract_hash,
              'thesis_code', NEW.thesis_code,
              'thesis_target_contract', NEW.thesis_target_contract,
              'thesis_target_hash', NEW.thesis_target_hash,
              'exposure_limit_contract', NEW.exposure_limit_contract,
              'exposure_limit_hash', NEW.exposure_limit_hash,
              'invalidation_codes', NEW.invalidation_codes,
              'frozen_at', NEW.plan_material->>'frozen_at')
       OR NEW.plan_material->>'frozen_at' !~ '\+00:00$'
       OR NEW.plan_material->>'evidence_window_start' !~ '\+00:00$'
       OR NEW.plan_material->>'evidence_window_end' !~ '\+00:00$'
       OR (NEW.plan_material->>'frozen_at')::timestamptz IS DISTINCT FROM NEW.frozen_at
       OR (NEW.plan_material->>'evidence_window_start')::timestamptz
              IS DISTINCT FROM NEW.evidence_window_start
       OR (NEW.plan_material->>'evidence_window_end')::timestamptz
              IS DISTINCT FROM NEW.evidence_window_end
       OR (prior.plan_id IS NULL AND NEW.version <> 1)
       OR (prior.plan_id IS NOT NULL AND (
              NEW.version <> prior.version + 1 OR NEW.frozen_at <= prior.frozen_at))
    THEN
        RAISE EXCEPTION 'DEVELOPMENT_OPPORTUNITY_PLAN_INVALID';
    END IF;
    RETURN NEW;
END $$;
