-- Bind the immutable submission baseline identifier into submission-role
-- opportunity baseline material while preserving the development payload.

ALTER TABLE entry_materialization_jobs
    DROP CONSTRAINT IF EXISTS entry_materialization_jobs_account_role_check,
    ADD CONSTRAINT ck_entry_materialization_job_executable_role
        CHECK (account_role IN ('DEVELOPMENT', 'SUBMISSION'));

CREATE OR REPLACE FUNCTION development_opportunity_baseline_guard()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    plan development_opportunity_plans%ROWTYPE;
    account account_roles%ROWTYPE;
    expected_material jsonb;
BEGIN
    SELECT * INTO plan
      FROM development_opportunity_plans
     WHERE plan_id = NEW.plan_id;
    SELECT * INTO account
      FROM account_roles
     WHERE role = NEW.account_role
     FOR UPDATE;

    expected_material := jsonb_build_object(
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
        'captured_at', NEW.baseline_material->>'captured_at'
    );
    IF NEW.account_role = 'SUBMISSION' THEN
        expected_material := expected_material || jsonb_build_object(
            'submission_baseline_id', NEW.submission_baseline_id::text
        );
    END IF;

    IF plan.plan_id IS NULL
       OR plan.account_role IS DISTINCT FROM NEW.account_role
       OR account.role IS NULL
       OR NEW.account_role NOT IN ('DEVELOPMENT', 'SUBMISSION')
       OR NEW.account_fingerprint IS DISTINCT FROM account.account_fingerprint
       OR NOT NEW.positions_complete OR NOT NEW.orders_complete OR NOT NEW.activity_complete
       OR NEW.captured_at < plan.frozen_at
       OR NEW.baseline_hash IS DISTINCT FROM opportunity_evidence_hash(
              'alphadecay.opportunity.baseline.v1', NEW.baseline_material)
       OR NEW.baseline_material IS DISTINCT FROM expected_material
       OR NEW.baseline_material->>'captured_at' !~ '\+00:00$'
       OR (NEW.baseline_material->>'captured_at')::timestamptz
              IS DISTINCT FROM NEW.captured_at
    THEN
        RAISE EXCEPTION 'DEVELOPMENT_OPPORTUNITY_BASELINE_INVALID';
    END IF;
    RETURN NEW;
END
$function$;
