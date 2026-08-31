CREATE OR REPLACE FUNCTION managed_position_transition_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    intent execution_intents%ROWTYPE; certificate execution_certificates%ROWTYPE;
    position managed_lifecycle_positions%ROWTYPE; reconciliation whole_account_reconciliations%ROWTYPE;
    approval entry_approval_certificates%ROWTYPE; assessment assessment_certificates%ROWTYPE;
    predecessor managed_position_transitions%ROWTYPE; predecessor_snapshot managed_position_snapshots%ROWTYPE;
    successor_snapshot managed_position_snapshots%ROWTYPE; market_session alpaca_market_sessions%ROWTYPE; prior_activities jsonb := '[]'::jsonb;
    reconciliation_state account_reconciliation_states%ROWTYPE; permit broker_mutation_permits%ROWTYPE;
    derived_activities jsonb; derived_cashflow numeric(18,6); derived_position_hash varchar(64);
BEGIN
    SELECT * INTO intent FROM execution_intents WHERE intent_id=NEW.execution_intent_id;
    SELECT * INTO certificate FROM execution_certificates WHERE certificate_id=NEW.execution_certificate_id;
    SELECT * INTO position FROM managed_lifecycle_positions WHERE managed_position_id=NEW.managed_position_id;
    SELECT * INTO reconciliation FROM whole_account_reconciliations WHERE reconciliation_id=NEW.post_reconciliation_id;
    SELECT * INTO market_session FROM alpaca_market_sessions WHERE market_session_id=NEW.market_session_id;
    SELECT * INTO approval FROM entry_approval_certificates WHERE approval_id=position.entry_approval_id;
    SELECT * INTO assessment FROM assessment_certificates WHERE certificate_id=intent.assessment_certificate_id;
    SELECT * INTO reconciliation_state FROM account_reconciliation_states
      WHERE authority_reconciliation_id=reconciliation.reconciliation_id;
    SELECT * INTO permit FROM broker_mutation_permits
      WHERE permit_id=reconciliation_state.authority_permit_id;
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
    derived_position_hash := lifecycle_position_fingerprint(COALESCE(reconciliation.sweep_payload->'final_positions','[]'::jsonb));
    IF intent.intent_id IS NULL OR certificate.certificate_id IS NULL OR position.managed_position_id IS NULL OR reconciliation.reconciliation_id IS NULL
       OR intent.state<>'TERMINAL' OR intent.action IS DISTINCT FROM NEW.action OR intent.account_role IS DISTINCT FROM position.account_role
       OR intent.policy_hash IS DISTINCT FROM (SELECT policy_hash FROM thesis_versions WHERE thesis_version_id=position.thesis_version_id)
       OR certificate.execution_intent_id IS DISTINCT FROM intent.intent_id OR certificate.execution_status<>'FILLED'
       OR certificate.reconciliation_id IS DISTINCT FROM reconciliation.reconciliation_id OR reconciliation.safe IS NOT TRUE
       OR jsonb_typeof(certificate.attempt_ids)<>'array'
       OR jsonb_array_length(certificate.attempt_ids)<>(SELECT count(*) FROM order_attempts attempt WHERE attempt.execution_intent_id=intent.intent_id)
       OR market_session.market_session_id IS NULL OR NEW.occurred_at<market_session.open_at OR NEW.occurred_at>market_session.close_at
       OR reconciliation.account_role IS DISTINCT FROM position.account_role OR reconciliation.account_fingerprint IS DISTINCT FROM position.account_fingerprint
       OR reconciliation.execution_intent_id IS DISTINCT FROM intent.intent_id OR NEW.cashflow_contribution IS DISTINCT FROM derived_cashflow
       OR EXISTS (SELECT 1 FROM order_attempts attempt WHERE attempt.execution_intent_id=intent.intent_id
            AND (NULLIF(attempt.client_order_id,'') IS NULL OR NOT (certificate.attempt_ids ? attempt.client_order_id)))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(certificate.attempt_ids) listed(client_order_id)
            WHERE jsonb_typeof(listed.client_order_id)<>'string'
               OR 1<>(SELECT count(*) FROM order_attempts attempt WHERE attempt.client_order_id=listed.client_order_id #>> '{}'
                AND attempt.execution_intent_id=intent.intent_id))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(derived_activities) activity
            WHERE 1<>(SELECT count(*) FROM order_attempts attempt WHERE attempt.execution_intent_id=intent.intent_id
                AND attempt.filled_quantity>0
                AND NULLIF(attempt.client_order_id,'') IS NOT NULL
                AND (NULLIF(attempt.provider_order_id,'') IS NULL OR attempt.provider_order_id=activity->>'provider_order_id')
                AND attempt.client_order_id=activity->>'client_order_id')
              OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(intent.legs) leg
                   WHERE leg->>'symbol'=activity->>'symbol'))
       OR EXISTS (
            SELECT 1
              FROM order_attempts attempt
              CROSS JOIN LATERAL jsonb_array_elements(intent.legs) leg
             WHERE attempt.execution_intent_id=intent.intent_id
               AND attempt.filled_quantity>0
               AND (
                    NULLIF(attempt.client_order_id,'') IS NULL
                    OR NULLIF(attempt.provider_order_id,'') IS NULL
                    OR COALESCE((
                        SELECT sum((activity->>'signed_quantity')::numeric)
                          FROM jsonb_array_elements(derived_activities) activity
                         WHERE activity->>'provider_order_id'=attempt.provider_order_id
                           AND activity->>'client_order_id'=attempt.client_order_id
                           AND activity->>'symbol'=leg->>'symbol'
                    ),0) IS DISTINCT FROM
                       attempt.filled_quantity * (leg->>'ratio')::integer
                       * CASE leg->>'intent'
                           WHEN 'BUY_TO_OPEN' THEN 1 WHEN 'BUY_TO_CLOSE' THEN 1
                           WHEN 'SELL_TO_OPEN' THEN -1 WHEN 'SELL_TO_CLOSE' THEN -1
                           ELSE 0 END
               )
       )
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
       OR assessment.valid IS NOT TRUE OR assessment.created_at>permit.issued_at
       OR permit.dispatch_acquired_at>=assessment.expires_at
       OR assessment.position_fingerprint IS DISTINCT FROM predecessor_snapshot.position_fingerprint
       OR assessment.created_at<market_session.open_at OR assessment.created_at>market_session.close_at
       OR assessment.policy_hash IS DISTINCT FROM intent.policy_hash OR assessment.thesis_version_id IS DISTINCT FROM position.thesis_version_id
       OR certificate.assessment_certificate_id IS DISTINCT FROM assessment.certificate_id
       OR successor_snapshot.snapshot_id IS NULL OR successor_snapshot.predecessor_snapshot_id IS DISTINCT FROM predecessor_snapshot.snapshot_id
       OR position.current_snapshot_id IS DISTINCT FROM successor_snapshot.snapshot_id
       OR position.current_reconciliation_state_id IS DISTINCT FROM successor_snapshot.reconciliation_state_id
       OR position.active_position_fingerprint IS DISTINCT FROM successor_snapshot.position_fingerprint
       OR jsonb_typeof(intent.legs) IS DISTINCT FROM 'array'
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(intent.legs) leg
            WHERE COALESCE(leg->>'symbol','') !~ '^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$'
               OR COALESCE(leg->>'ratio','') !~ '^[1-9][0-9]*$')
       OR (intent.action='CLOSE' AND (
            jsonb_array_length(intent.legs)<>2
            OR 2<>(SELECT count(*) FROM jsonb_array_elements(intent.legs) leg
                    WHERE leg->>'intent' IN ('BUY_TO_CLOSE','SELL_TO_CLOSE'))))
       OR (intent.action='ROLL' AND (
            jsonb_array_length(intent.legs)<>4
            OR 2<>(SELECT count(*) FROM jsonb_array_elements(intent.legs) leg
                    WHERE leg->>'intent' IN ('BUY_TO_CLOSE','SELL_TO_CLOSE'))
            OR 2<>(SELECT count(*) FROM jsonb_array_elements(intent.legs) leg
                    WHERE leg->>'intent' IN ('BUY_TO_OPEN','SELL_TO_OPEN'))
            OR 1<>(SELECT count(DISTINCT leg->>'ratio') FROM jsonb_array_elements(intent.legs) leg)))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(prior_activities) prior
            WHERE 1<>(SELECT count(*) FROM jsonb_array_elements(
                    COALESCE(reconciliation.sweep_payload->'activities','[]'::jsonb)) activity
                 WHERE activity->>'activity_id_hash'=prior->>'activity_id_hash'
                   AND activity=prior))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(derived_activities) activity
            WHERE COALESCE(activity->>'occurred_at','')=''
               OR (activity->>'occurred_at')::timestamptz<=predecessor_snapshot.accepted_at
               OR (activity->>'occurred_at')::timestamptz>reconciliation.accepted_at)
       OR (intent.action='ROLL' AND (
            jsonb_array_length(COALESCE(reconciliation.sweep_payload->'final_positions','[]'::jsonb))<>2
            OR 1<>(SELECT count(DISTINCT abs((item->>'signed_quantity')::numeric))
                    FROM jsonb_array_elements(reconciliation.sweep_payload->'final_positions') item)
            OR 1<>(SELECT count(*) FROM jsonb_array_elements(reconciliation.sweep_payload->'final_positions') item
                    WHERE (item->>'signed_quantity')::numeric>0)
            OR 1<>(SELECT count(*) FROM jsonb_array_elements(reconciliation.sweep_payload->'final_positions') item
                    WHERE (item->>'signed_quantity')::numeric<0)))
       OR (NEW.action='ROLL' AND position.closed_at IS NOT NULL)
       OR (NEW.action='CLOSE' AND position.closed_at IS DISTINCT FROM NEW.occurred_at))
    THEN RAISE EXCEPTION 'MANAGED_POSITION_TRANSITION_PREDECESSOR_INVALID'; END IF;
    IF (NEW.action='CLOSE') IS DISTINCT FROM (jsonb_array_length(COALESCE(reconciliation.sweep_payload->'final_positions','[]'::jsonb))=0)
    THEN RAISE EXCEPTION 'MANAGED_POSITION_TERMINAL_INVENTORY_INVALID'; END IF;
    RETURN NEW;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM managed_position_transitions WHERE transition_sequence>0
    ) THEN
        RAISE EXCEPTION 'LIFECYCLE_TERMINAL_CONTRACT_REQUIRES_ZERO_HISTORY';
    END IF;
END $$;
