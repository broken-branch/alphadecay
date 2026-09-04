-- Keep provider failures as append-only audit evidence without consuming the
-- single policy-decision slot for an opportunity boundary.

DO $migration$
DECLARE
    constraint_name text;
BEGIN
    SELECT constraint_record.conname INTO constraint_name
      FROM pg_constraint AS constraint_record
     WHERE constraint_record.conrelid = 'agent_input_snapshots'::regclass
       AND constraint_record.contype = 'u'
       AND pg_get_constraintdef(constraint_record.oid)
            ILIKE '%(account_role, decision_kind, decision_boundary)%';
    IF constraint_name IS NULL THEN
        RAISE EXCEPTION 'AGENT_INPUT_BOUNDARY_CONSTRAINT_MISSING';
    END IF;
    EXECUTE format('ALTER TABLE agent_input_snapshots DROP CONSTRAINT %I', constraint_name);
END
$migration$;

CREATE INDEX ix_agent_input_boundary
    ON agent_input_snapshots (account_role, decision_kind, decision_boundary);

CREATE UNIQUE INDEX uq_agent_policy_decision_boundary
    ON agent_decisions (account_role, decision_kind, decision_boundary)
    WHERE outcome NOT IN (
        'OPPORTUNITY_DECISION_PENDING',
        'PROVIDER_FAILURE_NO_TRADE',
        'PROVIDER_FAILURE_NO_ACTION'
    );

CREATE OR REPLACE FUNCTION submission_opportunity_decision_guard()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    input agent_input_snapshots%ROWTYPE;
    thesis thesis_versions%ROWTYPE;
    tick agent_ticks%ROWTYPE;
    origin jsonb;
BEGIN
    IF NEW.account_role <> 'SUBMISSION' OR NEW.decision_kind <> 'OPPORTUNITY' THEN
        RETURN NEW;
    END IF;

    SELECT * INTO input
      FROM agent_input_snapshots
     WHERE snapshot_id = NEW.input_snapshot_id;
    SELECT * INTO tick
      FROM agent_ticks
     WHERE tick_id = NEW.origin_tick_id;

    IF NEW.outcome = 'NO_TRADE'
       AND NEW.reason_code = 'CALIBRATION_BINDING_NO_TRADE'
       AND NOT NEW.autonomy_authorized
    THEN
        RETURN NEW;
    END IF;
    IF NEW.outcome IN (
           'NO_TRADE',
           'OPPORTUNITY_DECISION_PENDING',
           'PROVIDER_FAILURE_NO_TRADE'
       )
       AND NOT NEW.autonomy_authorized
    THEN
        IF input.snapshot_id IS NULL
           OR input.account_role <> 'SUBMISSION'
           OR input.account_role IS DISTINCT FROM NEW.account_role
           OR input.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
           OR input.decision_kind <> 'OPPORTUNITY'
           OR input.decision_boundary IS DISTINCT FROM NEW.decision_boundary
           OR tick.tick_id IS NULL
           OR tick.account_role IS DISTINCT FROM NEW.account_role
           OR tick.account_fingerprint IS DISTINCT FROM NEW.account_fingerprint
           OR tick.actor <> 'SCHEDULER'
           OR tick.decision_id IS DISTINCT FROM NEW.decision_id
        THEN
            RAISE EXCEPTION 'SUBMISSION_OPPORTUNITY_DECISION_LINEAGE_INVALID';
        END IF;
        RETURN NEW;
    END IF;

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
