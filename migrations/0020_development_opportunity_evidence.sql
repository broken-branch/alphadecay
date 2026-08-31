CREATE TABLE development_opportunity_plans (
    plan_id uuid PRIMARY KEY,
    opportunity_key varchar(80) NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    account_role varchar(16) NOT NULL REFERENCES account_roles(role)
        CHECK (account_role = 'DEVELOPMENT'),
    underlying varchar(6) NOT NULL CHECK (underlying ~ '^[A-Z]{1,6}$'),
    benchmark_symbol varchar(6) NOT NULL CHECK (benchmark_symbol = 'QQQ'),
    event_session date NOT NULL,
    pre_event_session date NOT NULL,
    reaction_session date NOT NULL,
    signal_session date NOT NULL,
    policy_payload jsonb NOT NULL CHECK (jsonb_typeof(policy_payload) = 'object'),
    policy_hash varchar(64) NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    request_contract jsonb NOT NULL CHECK (jsonb_typeof(request_contract) = 'object'),
    request_contract_hash varchar(64) NOT NULL
        CHECK (request_contract_hash ~ '^[0-9a-f]{64}$'),
    thesis_code varchar(64) NOT NULL CHECK (thesis_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
    thesis_target_contract jsonb NOT NULL
        CHECK (jsonb_typeof(thesis_target_contract) = 'object'),
    thesis_target_hash varchar(64) NOT NULL
        CHECK (thesis_target_hash ~ '^[0-9a-f]{64}$'),
    exposure_limit_contract jsonb NOT NULL
        CHECK (jsonb_typeof(exposure_limit_contract) = 'object'),
    exposure_limit_hash varchar(64) NOT NULL
        CHECK (exposure_limit_hash ~ '^[0-9a-f]{64}$'),
    invalidation_codes jsonb NOT NULL
        CHECK (jsonb_typeof(invalidation_codes) = 'array'
               AND jsonb_array_length(invalidation_codes) > 0),
    frozen_at timestamptz NOT NULL,
    plan_material jsonb NOT NULL CHECK (jsonb_typeof(plan_material) = 'object'),
    plan_hash varchar(64) NOT NULL UNIQUE CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
    CHECK (opportunity_key ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$'),
    CHECK (pre_event_session < event_session
           AND event_session < reaction_session
           AND reaction_session <= signal_session),
    UNIQUE (opportunity_key, version)
);

CREATE TABLE development_opportunity_baselines (
    baseline_id uuid PRIMARY KEY,
    plan_id uuid NOT NULL UNIQUE REFERENCES development_opportunity_plans(plan_id),
    account_role varchar(16) NOT NULL REFERENCES account_roles(role)
        CHECK (account_role = 'DEVELOPMENT'),
    account_fingerprint varchar(64) NOT NULL
        CHECK (account_fingerprint ~ '^[0-9a-f]{64}$'),
    account_source_hash varchar(64) NOT NULL
        CHECK (account_source_hash ~ '^[0-9a-f]{64}$'),
    positions_manifest jsonb NOT NULL CHECK (jsonb_typeof(positions_manifest) = 'array'),
    positions_source_hash varchar(64) NOT NULL
        CHECK (positions_source_hash ~ '^[0-9a-f]{64}$'),
    positions_complete boolean NOT NULL CHECK (positions_complete),
    orders_manifest jsonb NOT NULL CHECK (jsonb_typeof(orders_manifest) = 'array'),
    orders_source_hash varchar(64) NOT NULL
        CHECK (orders_source_hash ~ '^[0-9a-f]{64}$'),
    orders_complete boolean NOT NULL CHECK (orders_complete),
    activity_manifest jsonb NOT NULL CHECK (jsonb_typeof(activity_manifest) = 'array'),
    activity_source_hash varchar(64) NOT NULL
        CHECK (activity_source_hash ~ '^[0-9a-f]{64}$'),
    activity_complete boolean NOT NULL CHECK (activity_complete),
    book_hash varchar(64) NOT NULL CHECK (book_hash ~ '^[0-9a-f]{64}$'),
    history_hash varchar(64) NOT NULL CHECK (history_hash ~ '^[0-9a-f]{64}$'),
    captured_at timestamptz NOT NULL,
    baseline_material jsonb NOT NULL CHECK (jsonb_typeof(baseline_material) = 'object'),
    baseline_hash varchar(64) NOT NULL UNIQUE CHECK (baseline_hash ~ '^[0-9a-f]{64}$')
);

CREATE TABLE opportunity_observation_manifests (
    observation_id uuid PRIMARY KEY,
    plan_id uuid NOT NULL UNIQUE REFERENCES development_opportunity_plans(plan_id),
    baseline_id uuid NOT NULL UNIQUE REFERENCES development_opportunity_baselines(baseline_id),
    account_role varchar(16) NOT NULL REFERENCES account_roles(role)
        CHECK (account_role = 'DEVELOPMENT'),
    account_fingerprint varchar(64) NOT NULL
        CHECK (account_fingerprint ~ '^[0-9a-f]{64}$'),
    policy_hash varchar(64) NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    request_hash varchar(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    snapshot_hash varchar(64) NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    calendar_hash varchar(64) NOT NULL CHECK (calendar_hash ~ '^[0-9a-f]{64}$'),
    daily_hash varchar(64) NOT NULL CHECK (daily_hash ~ '^[0-9a-f]{64}$'),
    intraday_hash varchar(64) NOT NULL CHECK (intraday_hash ~ '^[0-9a-f]{64}$'),
    halt_hash varchar(64) NOT NULL CHECK (halt_hash ~ '^[0-9a-f]{64}$'),
    catalyst_hash varchar(64) NOT NULL CHECK (catalyst_hash ~ '^[0-9a-f]{64}$'),
    greek_hash varchar(64) NOT NULL CHECK (greek_hash ~ '^[0-9a-f]{64}$'),
    account_hash varchar(64) NOT NULL CHECK (account_hash ~ '^[0-9a-f]{64}$'),
    activity_hash varchar(64) NOT NULL CHECK (activity_hash ~ '^[0-9a-f]{64}$'),
    budget_hash varchar(64) NOT NULL CHECK (budget_hash ~ '^[0-9a-f]{64}$'),
    prior_decision_hash varchar(64) NOT NULL
        CHECK (prior_decision_hash ~ '^[0-9a-f]{64}$'),
    trusted_at timestamptz NOT NULL,
    evaluated_at timestamptz NOT NULL CHECK (evaluated_at >= trusted_at),
    observation_material jsonb NOT NULL
        CHECK (jsonb_typeof(observation_material) = 'object'),
    manifest_hash varchar(64) NOT NULL UNIQUE CHECK (manifest_hash ~ '^[0-9a-f]{64}$')
);

CREATE FUNCTION opportunity_canonical_json(value jsonb) RETURNS text
IMMUTABLE STRICT LANGUAGE plpgsql AS $$
DECLARE
    canonical text;
BEGIN
    CASE jsonb_typeof(value)
        WHEN 'object' THEN
            SELECT '{' || COALESCE(
                string_agg(to_jsonb(item.key)::text || ':' ||
                           opportunity_canonical_json(item.value), ',' ORDER BY item.key),
                ''
            ) || '}' INTO canonical
            FROM jsonb_each(value) AS item;
        WHEN 'array' THEN
            SELECT '[' || COALESCE(
                string_agg(opportunity_canonical_json(item.value), ',' ORDER BY item.ordinality),
                ''
            ) || ']' INTO canonical
            FROM jsonb_array_elements(value) WITH ORDINALITY AS item(value, ordinality);
        ELSE
            canonical := value::text;
    END CASE;
    RETURN canonical;
END $$;

CREATE FUNCTION opportunity_evidence_hash(domain text, value jsonb) RETURNS varchar(64)
IMMUTABLE STRICT LANGUAGE sql AS $$
    SELECT encode(
        sha256(convert_to(
            '{"domain":' || to_jsonb(domain)::text || ',"value":' ||
            opportunity_canonical_json(value) || '}',
            'UTF8'
        )),
        'hex'
    )
$$;

CREATE FUNCTION opportunity_plain_hash(value jsonb) RETURNS varchar(64)
IMMUTABLE STRICT LANGUAGE sql AS $$
    SELECT encode(
        sha256(convert_to(opportunity_canonical_json(value), 'UTF8')),
        'hex'
    )
$$;

CREATE FUNCTION opportunity_evidence_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'DEVELOPMENT_OPPORTUNITY_EVIDENCE_IMMUTABLE';
END $$;

CREATE FUNCTION development_opportunity_plan_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    prior development_opportunity_plans%ROWTYPE;
BEGIN
    PERFORM 1 FROM account_roles WHERE role = 'DEVELOPMENT' FOR UPDATE;
    SELECT * INTO prior FROM development_opportunity_plans
     WHERE opportunity_key = NEW.opportunity_key
     ORDER BY version DESC LIMIT 1;
    IF NEW.account_role <> 'DEVELOPMENT'
       OR NEW.benchmark_symbol <> 'QQQ'
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
       OR (NEW.plan_material->>'frozen_at')::timestamptz IS DISTINCT FROM NEW.frozen_at
       OR (prior.plan_id IS NULL AND NEW.version <> 1)
       OR (prior.plan_id IS NOT NULL AND (
              NEW.version <> prior.version + 1 OR NEW.frozen_at <= prior.frozen_at))
    THEN
        RAISE EXCEPTION 'DEVELOPMENT_OPPORTUNITY_PLAN_INVALID';
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION development_opportunity_baseline_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    plan development_opportunity_plans%ROWTYPE;
    account account_roles%ROWTYPE;
BEGIN
    SELECT * INTO plan FROM development_opportunity_plans WHERE plan_id = NEW.plan_id;
    SELECT * INTO account FROM account_roles WHERE role = 'DEVELOPMENT' FOR UPDATE;
    IF plan.plan_id IS NULL
       OR plan.account_role <> 'DEVELOPMENT'
       OR account.role IS NULL
       OR NEW.account_role <> 'DEVELOPMENT'
       OR NEW.account_fingerprint IS DISTINCT FROM account.account_fingerprint
       OR NOT NEW.positions_complete OR NOT NEW.orders_complete OR NOT NEW.activity_complete
       OR NEW.captured_at < plan.frozen_at
       OR NEW.baseline_hash IS DISTINCT FROM opportunity_evidence_hash(
              'alphadecay.opportunity.baseline.v1', NEW.baseline_material)
       OR NEW.baseline_material IS DISTINCT FROM jsonb_build_object(
              'plan_id', NEW.plan_id::text,
              'account_role', NEW.account_role,
              'account_fingerprint', NEW.account_fingerprint,
              'account_source_hash', NEW.account_source_hash,
              'positions_manifest', NEW.positions_manifest,
              'positions_source_hash', NEW.positions_source_hash,
              'positions_complete', NEW.positions_complete,
              'orders_manifest', NEW.orders_manifest,
              'orders_source_hash', NEW.orders_source_hash,
              'orders_complete', NEW.orders_complete,
              'activity_manifest', NEW.activity_manifest,
              'activity_source_hash', NEW.activity_source_hash,
              'activity_complete', NEW.activity_complete,
              'book_hash', NEW.book_hash,
              'history_hash', NEW.history_hash,
              'captured_at', NEW.baseline_material->>'captured_at')
       OR NEW.baseline_material->>'captured_at' !~ '\+00:00$'
       OR (NEW.baseline_material->>'captured_at')::timestamptz IS DISTINCT FROM NEW.captured_at
    THEN
        RAISE EXCEPTION 'DEVELOPMENT_OPPORTUNITY_BASELINE_INVALID';
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION opportunity_observation_manifest_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    plan development_opportunity_plans%ROWTYPE;
    baseline development_opportunity_baselines%ROWTYPE;
BEGIN
    SELECT * INTO plan FROM development_opportunity_plans WHERE plan_id = NEW.plan_id;
    SELECT * INTO baseline FROM development_opportunity_baselines
     WHERE baseline_id = NEW.baseline_id;
    IF plan.plan_id IS NULL OR baseline.baseline_id IS NULL
       OR baseline.plan_id IS DISTINCT FROM plan.plan_id
       OR NEW.account_role <> 'DEVELOPMENT'
       OR NEW.account_role IS DISTINCT FROM baseline.account_role
       OR NEW.account_fingerprint IS DISTINCT FROM baseline.account_fingerprint
       OR NEW.policy_hash IS DISTINCT FROM plan.policy_hash
       OR NEW.trusted_at < plan.frozen_at
       OR NEW.trusted_at < baseline.captured_at
       OR NEW.evaluated_at < NEW.trusted_at
       OR NEW.manifest_hash IS DISTINCT FROM opportunity_evidence_hash(
              'alphadecay.opportunity.observation.v1', NEW.observation_material)
       OR NEW.observation_material IS DISTINCT FROM jsonb_build_object(
              'plan_id', NEW.plan_id::text,
              'baseline_id', NEW.baseline_id::text,
              'account_role', NEW.account_role,
              'account_fingerprint', NEW.account_fingerprint,
              'policy_hash', NEW.policy_hash,
              'request_hash', NEW.request_hash,
              'snapshot_hash', NEW.snapshot_hash,
              'calendar_hash', NEW.calendar_hash,
              'daily_hash', NEW.daily_hash,
              'intraday_hash', NEW.intraday_hash,
              'halt_hash', NEW.halt_hash,
              'catalyst_hash', NEW.catalyst_hash,
              'greek_hash', NEW.greek_hash,
              'account_hash', NEW.account_hash,
              'activity_hash', NEW.activity_hash,
              'budget_hash', NEW.budget_hash,
              'prior_decision_hash', NEW.prior_decision_hash,
              'trusted_at', NEW.observation_material->>'trusted_at',
              'evaluated_at', NEW.observation_material->>'evaluated_at')
       OR NEW.observation_material->>'trusted_at' !~ '\+00:00$'
       OR NEW.observation_material->>'evaluated_at' !~ '\+00:00$'
       OR (NEW.observation_material->>'trusted_at')::timestamptz
              IS DISTINCT FROM NEW.trusted_at
       OR (NEW.observation_material->>'evaluated_at')::timestamptz
              IS DISTINCT FROM NEW.evaluated_at
    THEN
        RAISE EXCEPTION 'OPPORTUNITY_OBSERVATION_MANIFEST_INVALID';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER development_opportunity_plan_insert_guard
BEFORE INSERT ON development_opportunity_plans
FOR EACH ROW EXECUTE FUNCTION development_opportunity_plan_guard();

CREATE CONSTRAINT TRIGGER development_opportunity_baseline_insert_guard
AFTER INSERT ON development_opportunity_baselines DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION development_opportunity_baseline_guard();

CREATE CONSTRAINT TRIGGER opportunity_observation_manifest_insert_guard
AFTER INSERT ON opportunity_observation_manifests DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION opportunity_observation_manifest_guard();

CREATE TRIGGER development_opportunity_plan_append_only
BEFORE UPDATE OR DELETE ON development_opportunity_plans
FOR EACH ROW EXECUTE FUNCTION opportunity_evidence_append_only();

CREATE TRIGGER development_opportunity_baseline_append_only
BEFORE UPDATE OR DELETE ON development_opportunity_baselines
FOR EACH ROW EXECUTE FUNCTION opportunity_evidence_append_only();

CREATE TRIGGER opportunity_observation_manifest_append_only
BEFORE UPDATE OR DELETE ON opportunity_observation_manifests
FOR EACH ROW EXECUTE FUNCTION opportunity_evidence_append_only();
