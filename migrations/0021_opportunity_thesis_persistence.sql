DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM opportunity_observation_manifests) THEN
        RAISE EXCEPTION 'OPPORTUNITY_SIGNAL_AUTHORITY_MIGRATION_REQUIRES_ZERO_OBSERVATIONS';
    END IF;
END $$;

ALTER TABLE opportunity_observation_manifests
    ADD COLUMN signal_authority_hash varchar(64) NOT NULL
    CHECK (signal_authority_hash ~ '^[0-9a-f]{64}$');

CREATE OR REPLACE FUNCTION opportunity_observation_manifest_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
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
              'signal_authority_hash', NEW.signal_authority_hash,
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

ALTER TABLE thesis_versions ADD COLUMN origin_hash varchar(64);
DROP TRIGGER thesis_versions_append_only ON thesis_versions;
UPDATE thesis_versions
   SET origin_hash = COALESCE(thesis_payload->>'origin_hash', thesis_hash);
ALTER TABLE thesis_versions ALTER COLUMN origin_hash SET NOT NULL;
ALTER TABLE thesis_versions ADD CONSTRAINT uq_thesis_origin_hash UNIQUE (origin_hash);
ALTER TABLE thesis_versions ADD CONSTRAINT ck_thesis_origin_hash
    CHECK (origin_hash ~ '^[0-9a-f]{64}$');
ALTER TABLE thesis_versions ADD CONSTRAINT ck_thesis_created_after_freeze
    CHECK (created_at >= frozen_at);
CREATE TRIGGER thesis_versions_append_only BEFORE UPDATE OR DELETE ON thesis_versions
FOR EACH ROW EXECUTE FUNCTION lifecycle_authority_append_only();

DROP TRIGGER thesis_versions_derive_hash ON thesis_versions;
CREATE OR REPLACE FUNCTION lifecycle_derive_thesis_hash() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE material jsonb;
DECLARE origin jsonb;
BEGIN
    IF NEW.origin_hash IS NULL THEN
        NEW.origin_hash := NEW.thesis_hash;
    END IF;
    material := jsonb_build_object(
        'thesis_version_id', NEW.thesis_version_id::text,
        'thesis_id', NEW.thesis_id::text,
        'account_role', NEW.account_role,
        'version', NEW.version,
        'origin_hash', NEW.origin_hash,
        'policy_hash', NEW.policy_hash,
        'underlying', NEW.underlying,
        'thesis_code', NEW.thesis_code,
        'frozen_at', to_char(NEW.frozen_at AT TIME ZONE 'UTC',
                             'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'target_at', to_char(NEW.target_at AT TIME ZONE 'UTC',
                             'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'intended_exposure', NEW.intended_exposure,
        'exposure_limits', NEW.exposure_limits,
        'volatility_view', NEW.volatility_view,
        'entry_atm_iv', trim_scale(NEW.entry_atm_iv)::text,
        'approved_max_loss', trim_scale(NEW.approved_max_loss)::text,
        'portfolio_risk_cap', trim_scale(NEW.portfolio_risk_cap)::text,
        'invalidation_codes', NEW.invalidation_codes,
        'thesis_payload', NEW.thesis_payload - 'thesis_hash'
    );
    NEW.thesis_hash := opportunity_evidence_hash('alphadecay.lifecycle.thesis.v2', material);
    IF NEW.thesis_payload ? 'origin_hash' THEN
        origin := NEW.thesis_payload->'origin_material';
        IF jsonb_typeof(origin) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'THESIS_PAYLOAD_HASH_MISMATCH';
        END IF;
        IF (SELECT count(*) FROM jsonb_object_keys(origin)) <> 28
           OR opportunity_evidence_hash(
                'alphadecay.opportunity.thesis-origin.v1', origin)
                IS DISTINCT FROM NEW.origin_hash
           OR NEW.thesis_payload->>'origin_hash' IS DISTINCT FROM NEW.origin_hash
           OR EXISTS (
                SELECT 1 FROM unnest(ARRAY[
                    'plan_hash', 'baseline_hash', 'observation_hash',
                    'account_fingerprint', 'policy_hash', 'input_authority_hash',
                    'signal_authority_hash', 'decision_hash', 'candidate_hash',
                    'atm_call_source_hash', 'atm_put_source_hash'
                ]) AS item(key)
                WHERE jsonb_typeof(origin->item.key) IS DISTINCT FROM 'string'
                   OR origin->>item.key !~ '^[0-9a-f]{64}$')
           OR EXISTS (
                SELECT 1 FROM unnest(ARRAY['plan_id', 'baseline_id', 'observation_id'])
                    AS item(key)
                WHERE jsonb_typeof(origin->item.key) IS DISTINCT FROM 'string'
                   OR origin->>item.key !~
                        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR jsonb_typeof(origin->'selected_option_sources') IS DISTINCT FROM 'array'
           OR jsonb_typeof(origin->'opportunity_key') IS DISTINCT FROM 'string'
           OR origin->>'opportunity_key' !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$'
           OR origin->>'account_fingerprint' IS DISTINCT FROM (
                SELECT account_fingerprint FROM account_roles WHERE role = 'DEVELOPMENT')
           OR origin->>'underlying' IS DISTINCT FROM NEW.underlying
           OR origin->>'policy_hash' IS DISTINCT FROM NEW.policy_hash
           OR origin->>'thesis_code' IS DISTINCT FROM NEW.thesis_code
           OR origin->>'target_at' IS DISTINCT FROM to_char(
                NEW.target_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
           OR origin->'intended_exposure' IS DISTINCT FROM NEW.intended_exposure
           OR origin->'exposure_limits' IS DISTINCT FROM NEW.exposure_limits
           OR origin->>'volatility_view' IS DISTINCT FROM NEW.volatility_view
           OR origin->>'entry_atm_iv' IS DISTINCT FROM trim_scale(NEW.entry_atm_iv)::text
           OR origin->>'approved_max_loss' IS DISTINCT FROM
                trim_scale(NEW.approved_max_loss)::text
           OR origin->>'portfolio_risk_cap' IS DISTINCT FROM
                trim_scale(NEW.portfolio_risk_cap)::text
           OR origin->'invalidation_codes' IS DISTINCT FROM NEW.invalidation_codes
           OR origin->>'frozen_at' IS DISTINCT FROM to_char(
                NEW.frozen_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
           OR jsonb_typeof(origin->'decision_boundary') IS DISTINCT FROM 'string'
           OR origin->>'decision_boundary' !~
                '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$'
           OR NEW.thesis_payload IS DISTINCT FROM jsonb_build_object(
                'schema_version', 'v1',
                'thesis_id', NEW.thesis_id::text,
                'version', NEW.version,
                'frozen', true,
                'thesis_hash', NEW.thesis_hash,
                'thesis', jsonb_build_object(
                    'schema_version', 'v1',
                    'underlying', NEW.underlying,
                    'thesis_code', NEW.thesis_code,
                    'invalidation_codes', NEW.invalidation_codes,
                    'intended_exposure', NEW.intended_exposure,
                    'source_policy_hash', NEW.policy_hash),
                'origin_hash', NEW.origin_hash,
                'origin_material', origin)
        THEN
            RAISE EXCEPTION 'THESIS_PAYLOAD_HASH_MISMATCH';
        END IF;
        IF NOT EXISTS (
                SELECT 1 FROM development_opportunity_plans plan
                WHERE plan.plan_id = (origin->>'plan_id')::uuid
                  AND plan.plan_hash = origin->>'plan_hash'
                  AND plan.account_role = 'DEVELOPMENT'
                  AND plan.opportunity_key = origin->>'opportunity_key'
                  AND plan.underlying = NEW.underlying
                  AND plan.policy_hash = NEW.policy_hash
                  AND plan.thesis_code = NEW.thesis_code)
           OR NOT EXISTS (
                SELECT 1 FROM development_opportunity_baselines baseline
                WHERE baseline.baseline_id = (origin->>'baseline_id')::uuid
                  AND baseline.plan_id = (origin->>'plan_id')::uuid
                  AND baseline.baseline_hash = origin->>'baseline_hash'
                  AND baseline.account_role = 'DEVELOPMENT'
                  AND baseline.account_fingerprint = origin->>'account_fingerprint')
           OR NOT EXISTS (
                SELECT 1 FROM opportunity_observation_manifests observation
                WHERE observation.observation_id = (origin->>'observation_id')::uuid
                  AND observation.plan_id = (origin->>'plan_id')::uuid
                  AND observation.baseline_id = (origin->>'baseline_id')::uuid
                  AND observation.manifest_hash = origin->>'observation_hash'
                  AND observation.account_role = 'DEVELOPMENT'
                  AND observation.account_fingerprint = origin->>'account_fingerprint'
                  AND observation.policy_hash = NEW.policy_hash
                  AND observation.signal_authority_hash =
                        origin->>'signal_authority_hash')
        THEN
            RAISE EXCEPTION 'THESIS_SOURCE_AUTHORITY_MISMATCH';
        END IF;
        IF jsonb_array_length(origin->'selected_option_sources') <> 2
           OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(origin->'selected_option_sources') item
                WHERE jsonb_typeof(item) IS DISTINCT FROM 'string'
                   OR item #>> '{}' !~ '^[0-9a-f]{64}$')
        THEN
            RAISE EXCEPTION 'THESIS_PAYLOAD_HASH_MISMATCH';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER thesis_versions_derive_hash BEFORE INSERT ON thesis_versions
FOR EACH ROW EXECUTE FUNCTION lifecycle_derive_thesis_hash();
