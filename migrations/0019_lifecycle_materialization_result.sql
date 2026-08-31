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
                      WHEN NEW.terminal_code IN (
                          'ENTRY_FILLED_MATERIALIZATION_FAILED',
                          'LIFECYCLE_FILLED_MATERIALIZATION_FAILED'
                      ) THEN 'FILLED'
                      ELSE NEW.terminal_code
                  END
                  AND (
                      (intent.action = 'ENTRY'
                       AND intent.entry_approval_id IS NOT NULL
                       AND intent.assessment_certificate_id IS NULL
                       AND certificate.entry_approval_id = intent.entry_approval_id
                       AND certificate.assessment_certificate_id IS NULL)
                      OR
                      (intent.action IN ('CLOSE', 'ROLL')
                       AND intent.entry_approval_id IS NULL
                       AND intent.assessment_certificate_id IS NOT NULL
                       AND assessment_authorization.action = intent.action
                       AND certificate.entry_approval_id IS NULL
                       AND certificate.assessment_certificate_id
                           = intent.assessment_certificate_id)
                  )
                  AND (
                      (NEW.terminal_code = 'ENTRY_FILLED_MATERIALIZATION_FAILED'
                       AND intent.action = 'ENTRY')
                      OR
                      (NEW.terminal_code = 'LIFECYCLE_FILLED_MATERIALIZATION_FAILED'
                       AND intent.action IN ('CLOSE', 'ROLL'))
                      OR NEW.terminal_code NOT IN (
                          'ENTRY_FILLED_MATERIALIZATION_FAILED',
                          'LIFECYCLE_FILLED_MATERIALIZATION_FAILED'
                      )
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
            'ENTRY_FILLED_MATERIALIZATION_FAILED',
            'LIFECYCLE_FILLED_MATERIALIZATION_FAILED'
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
