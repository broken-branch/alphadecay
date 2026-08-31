-- Admit role-bound submission opportunity and lifecycle evidence without enabling
-- the separately gated submission acquisition runtime.

DO $migration$
DECLARE
    relation_name text;
    constraint_name text;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'development_opportunity_plans',
        'development_opportunity_baselines',
        'opportunity_observation_manifests',
        'lifecycle_source_observations',
        'lifecycle_account_observations'
    ]
    LOOP
        FOR constraint_name IN
            SELECT constraint_record.conname
              FROM pg_constraint AS constraint_record
             WHERE constraint_record.conrelid = relation_name::regclass
               AND constraint_record.contype = 'c'
               AND pg_get_constraintdef(constraint_record.oid) ILIKE '%DEVELOPMENT%'
        LOOP
            EXECUTE format(
                'ALTER TABLE %I DROP CONSTRAINT %I',
                relation_name,
                constraint_name
            );
        END LOOP;
    END LOOP;
END
$migration$;

ALTER TABLE development_opportunity_plans
    DROP CONSTRAINT IF EXISTS development_opportunity_plans_opportunity_key_version_key,
    ADD CONSTRAINT ck_opportunity_plan_executable_role
        CHECK (account_role IN ('DEVELOPMENT', 'SUBMISSION')),
    ADD CONSTRAINT uq_opportunity_plan_role_version
        UNIQUE (account_role, opportunity_key, version);

ALTER TABLE development_opportunity_baselines
    ADD COLUMN submission_baseline_id uuid
        REFERENCES submission_baselines(baseline_id),
    ADD CONSTRAINT ck_opportunity_baseline_executable_role
        CHECK (account_role IN ('DEVELOPMENT', 'SUBMISSION')),
    ADD CONSTRAINT ck_opportunity_submission_baseline_binding
        CHECK (
            (account_role = 'DEVELOPMENT' AND submission_baseline_id IS NULL)
            OR
            (account_role = 'SUBMISSION' AND submission_baseline_id IS NOT NULL)
        );

ALTER TABLE opportunity_observation_manifests
    ADD CONSTRAINT ck_opportunity_manifest_executable_role
        CHECK (account_role IN ('DEVELOPMENT', 'SUBMISSION'));

ALTER TABLE lifecycle_source_observations
    ADD CONSTRAINT ck_lifecycle_source_executable_role
        CHECK (account_role IS NULL OR account_role IN ('DEVELOPMENT', 'SUBMISSION'));

ALTER TABLE lifecycle_account_observations
    ADD CONSTRAINT ck_lifecycle_account_executable_role
        CHECK (account_role IN ('DEVELOPMENT', 'SUBMISSION'));

CREATE OR REPLACE FUNCTION rewrite_authority_function(
    target regprocedure,
    old_fragment text,
    new_fragment text
) RETURNS void LANGUAGE plpgsql AS $migration$
DECLARE
    definition text;
    rewritten text;
BEGIN
    SELECT pg_get_functiondef(target) INTO definition;
    rewritten := replace(definition, old_fragment, new_fragment);
    IF rewritten IS NOT DISTINCT FROM definition THEN
        RAISE EXCEPTION 'AUTHORITY_FUNCTION_REWRITE_FRAGMENT_MISSING: %', old_fragment;
    END IF;
    EXECUTE rewritten;
END
$migration$;

SELECT rewrite_authority_function(
    'development_opportunity_plan_guard()'::regprocedure,
    'WHERE role = ''DEVELOPMENT'' FOR UPDATE',
    'WHERE role = NEW.account_role FOR UPDATE'
);
SELECT rewrite_authority_function(
    'development_opportunity_plan_guard()'::regprocedure,
    'WHERE opportunity_key = NEW.opportunity_key',
    'WHERE account_role = NEW.account_role AND opportunity_key = NEW.opportunity_key'
);
SELECT rewrite_authority_function(
    'development_opportunity_plan_guard()'::regprocedure,
    'NEW.account_role <> ''DEVELOPMENT''',
    'NEW.account_role NOT IN (''DEVELOPMENT'', ''SUBMISSION'')'
);
SELECT rewrite_authority_function(
    'development_opportunity_plan_guard()'::regprocedure,
    'SELECT account_fingerprint FROM account_roles WHERE role = ''DEVELOPMENT''',
    'SELECT account_fingerprint FROM account_roles WHERE role = NEW.account_role'
);
SELECT rewrite_authority_function(
    'development_opportunity_plan_guard()'::regprocedure,
    'OR NEW.request_contract->>''underlying'' IS DISTINCT FROM NEW.underlying',
    'OR NEW.request_contract->>''account_role'' IS DISTINCT FROM NEW.account_role
       OR NEW.request_contract->>''underlying'' IS DISTINCT FROM NEW.underlying'
);
SELECT rewrite_authority_function(
    'development_opportunity_plan_guard()'::regprocedure,
    '(SELECT count(*) FROM jsonb_object_keys(NEW.request_contract)) <> 11',
    '(SELECT count(*) FROM jsonb_object_keys(NEW.request_contract)) <> 12'
);
SELECT rewrite_authority_function(
    'development_opportunity_plan_guard()'::regprocedure,
    '''account_fingerprint'', ''underlying'', ''benchmark'', ''decision_boundary'',',
    '''account_role'', ''account_fingerprint'', ''underlying'', ''benchmark'', ''decision_boundary'','
);

SELECT rewrite_authority_function(
    'development_opportunity_baseline_guard()'::regprocedure,
    'WHERE role = ''DEVELOPMENT'' FOR UPDATE',
    'WHERE role = NEW.account_role FOR UPDATE'
);
SELECT rewrite_authority_function(
    'development_opportunity_baseline_guard()'::regprocedure,
    'plan.account_role <> ''DEVELOPMENT''',
    'plan.account_role IS DISTINCT FROM NEW.account_role'
);
SELECT rewrite_authority_function(
    'development_opportunity_baseline_guard()'::regprocedure,
    'NEW.account_role <> ''DEVELOPMENT''',
    'NEW.account_role NOT IN (''DEVELOPMENT'', ''SUBMISSION'')'
);

SELECT rewrite_authority_function(
    'opportunity_observation_manifest_guard()'::regprocedure,
    'NEW.account_role <> ''DEVELOPMENT''',
    'NEW.account_role NOT IN (''DEVELOPMENT'', ''SUBMISSION'')'
);

SELECT rewrite_authority_function(
    'lifecycle_derive_thesis_hash()'::regprocedure,
    'WHERE role = ''DEVELOPMENT''',
    'WHERE role = NEW.account_role'
);
SELECT rewrite_authority_function(
    'lifecycle_derive_thesis_hash()'::regprocedure,
    'plan.account_role = ''DEVELOPMENT''',
    'plan.account_role = NEW.account_role'
);
SELECT rewrite_authority_function(
    'lifecycle_derive_thesis_hash()'::regprocedure,
    'baseline.account_role = ''DEVELOPMENT''',
    'baseline.account_role = NEW.account_role'
);
SELECT rewrite_authority_function(
    'lifecycle_derive_thesis_hash()'::regprocedure,
    'observation.account_role = ''DEVELOPMENT''',
    'observation.account_role = NEW.account_role'
);

SELECT rewrite_authority_function(
    'lifecycle_provider_manifest_guard()'::regprocedure,
    'position.account_role<>''DEVELOPMENT''',
    'position.account_role NOT IN (''DEVELOPMENT'',''SUBMISSION'')'
);
SELECT rewrite_authority_function(
    'lifecycle_observation_binding_guard()'::regprocedure,
    'input.account_role<>''DEVELOPMENT''',
    'input.account_role NOT IN (''DEVELOPMENT'',''SUBMISSION'')'
);

DROP FUNCTION rewrite_authority_function(regprocedure, text, text);

CREATE OR REPLACE FUNCTION submission_opportunity_baseline_guard()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    plan development_opportunity_plans%ROWTYPE;
    account account_roles%ROWTYPE;
    submission_record submission_baselines%ROWTYPE;
BEGIN
    SELECT * INTO plan
      FROM development_opportunity_plans
     WHERE plan_id = NEW.plan_id;
    SELECT * INTO account
      FROM account_roles
     WHERE role = NEW.account_role
     FOR UPDATE;

    IF plan.plan_id IS NULL
       OR account.role IS NULL
       OR plan.account_role IS DISTINCT FROM NEW.account_role
       OR account.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
    THEN
        RAISE EXCEPTION 'OPPORTUNITY_BASELINE_ROLE_AUTHORITY_INVALID';
    END IF;

    IF NEW.account_role = 'DEVELOPMENT' THEN
        IF NEW.submission_baseline_id IS NOT NULL THEN
            RAISE EXCEPTION 'DEVELOPMENT_SUBMISSION_BASELINE_FORBIDDEN';
        END IF;
        RETURN NEW;
    END IF;

    SELECT * INTO submission_record
      FROM submission_baselines
     WHERE baseline_id = NEW.submission_baseline_id
     FOR SHARE;
    IF NEW.account_role <> 'SUBMISSION'
       OR submission_record.baseline_id IS NULL
       OR submission_record.account_role <> 'SUBMISSION'
       OR submission_record.contaminated
       OR submission_record.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
       OR submission_record.account_fingerprint IS DISTINCT FROM account.account_fingerprint
    THEN
        RAISE EXCEPTION 'SUBMISSION_OPPORTUNITY_BASELINE_INVALID';
    END IF;
    RETURN NEW;
END
$function$;

CREATE CONSTRAINT TRIGGER submission_opportunity_baseline_guard
AFTER INSERT ON development_opportunity_baselines
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION submission_opportunity_baseline_guard();

CREATE OR REPLACE FUNCTION submission_opportunity_observation_guard()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    baseline development_opportunity_baselines%ROWTYPE;
    submission_record submission_baselines%ROWTYPE;
BEGIN
    SELECT * INTO baseline
      FROM development_opportunity_baselines
     WHERE baseline_id = NEW.baseline_id;
    IF baseline.baseline_id IS NULL
       OR baseline.account_role IS DISTINCT FROM NEW.account_role
       OR baseline.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
    THEN
        RAISE EXCEPTION 'OPPORTUNITY_OBSERVATION_ROLE_AUTHORITY_INVALID';
    END IF;
    IF NEW.account_role = 'SUBMISSION' THEN
        SELECT * INTO submission_record
          FROM submission_baselines
         WHERE baseline_id = baseline.submission_baseline_id
         FOR SHARE;
        IF submission_record.baseline_id IS NULL
           OR submission_record.contaminated
           OR submission_record.account_role <> 'SUBMISSION'
           OR submission_record.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
        THEN
            RAISE EXCEPTION 'SUBMISSION_OPPORTUNITY_BASELINE_INVALID';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE CONSTRAINT TRIGGER submission_opportunity_observation_guard
AFTER INSERT ON opportunity_observation_manifests
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION submission_opportunity_observation_guard();

DO $migration$
DECLARE
    constraint_name text;
BEGIN
    SELECT constraint_record.conname INTO constraint_name
      FROM pg_constraint AS constraint_record
     WHERE constraint_record.conrelid = 'agent_decisions'::regclass
       AND constraint_record.contype = 'c'
       AND pg_get_constraintdef(constraint_record.oid)
            ILIKE '%CALIBRATION_BINDING_NO_TRADE%';
    IF constraint_name IS NULL THEN
        RAISE EXCEPTION 'SUBMISSION_OPPORTUNITY_DECISION_CONSTRAINT_MISSING';
    END IF;
    EXECUTE format('ALTER TABLE agent_decisions DROP CONSTRAINT %I', constraint_name);
END
$migration$;

CREATE OR REPLACE FUNCTION submission_opportunity_decision_guard()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    input agent_input_snapshots%ROWTYPE;
    thesis thesis_versions%ROWTYPE;
    origin jsonb;
BEGIN
    IF NEW.account_role <> 'SUBMISSION' OR NEW.decision_kind <> 'OPPORTUNITY' THEN
        RETURN NEW;
    END IF;
    IF NEW.outcome = 'NO_TRADE'
       AND NEW.reason_code = 'CALIBRATION_BINDING_NO_TRADE'
       AND NOT NEW.autonomy_authorized
    THEN
        RETURN NEW;
    END IF;

    SELECT * INTO input
      FROM agent_input_snapshots
     WHERE snapshot_id = NEW.input_snapshot_id;
    SELECT * INTO thesis
      FROM thesis_versions
     WHERE thesis_version_id = NEW.thesis_version_id;
    origin := thesis.thesis_payload->'origin_material';
    IF input.snapshot_id IS NULL
       OR thesis.thesis_version_id IS NULL
       OR input.account_role <> 'SUBMISSION'
       OR input.account_role IS DISTINCT FROM NEW.account_role
       OR input.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
       OR input.decision_kind <> 'OPPORTUNITY'
       OR thesis.account_role <> 'SUBMISSION'
       OR origin->>'account_fingerprint' IS DISTINCT FROM NEW.account_fingerprint
       OR NOT EXISTS (
            SELECT 1
              FROM development_opportunity_plans plan
              JOIN development_opportunity_baselines baseline
                ON baseline.plan_id = plan.plan_id
              JOIN opportunity_observation_manifests observation
                ON observation.plan_id = plan.plan_id
               AND observation.baseline_id = baseline.baseline_id
             WHERE plan.plan_id = (origin->>'plan_id')::uuid
               AND baseline.baseline_id = (origin->>'baseline_id')::uuid
               AND observation.observation_id = (origin->>'observation_id')::uuid
               AND plan.account_role = 'SUBMISSION'
               AND baseline.account_role = plan.account_role
               AND observation.account_role = plan.account_role
               AND baseline.account_fingerprint = NEW.account_fingerprint
               AND observation.account_fingerprint = NEW.account_fingerprint
               AND EXISTS (
                    SELECT 1
                      FROM submission_baselines submission_record
                     WHERE submission_record.baseline_id = baseline.submission_baseline_id
                       AND submission_record.account_role = 'SUBMISSION'
                       AND NOT submission_record.contaminated
                       AND submission_record.account_fingerprint = NEW.account_fingerprint
               )
       )
    THEN
        RAISE EXCEPTION 'SUBMISSION_OPPORTUNITY_DECISION_LINEAGE_INVALID';
    END IF;
    RETURN NEW;
END
$function$;

CREATE CONSTRAINT TRIGGER submission_opportunity_decision_guard
AFTER INSERT ON agent_decisions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION submission_opportunity_decision_guard();
