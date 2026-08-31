LOCK TABLE managed_lifecycle_positions, managed_position_transitions,
    managed_position_snapshots IN ACCESS EXCLUSIVE MODE;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM managed_lifecycle_positions)
       OR EXISTS (SELECT 1 FROM managed_position_transitions)
       OR EXISTS (SELECT 1 FROM managed_position_snapshots)
    THEN RAISE EXCEPTION 'MANAGED_POSITION_LINEAGE_CONTRACT_REQUIRES_ZERO_HISTORY'; END IF;
END $$;

CREATE FUNCTION lifecycle_position_fingerprint(inventory jsonb)
RETURNS varchar(64) IMMUTABLE STRICT LANGUAGE plpgsql AS $$
DECLARE
    item jsonb;
    canonical text := '[';
    separator text := '';
BEGIN
    IF jsonb_typeof(inventory) <> 'array' OR jsonb_array_length(inventory) > 64 THEN
        RAISE EXCEPTION 'MANAGED_POSITION_FINGERPRINT_INVENTORY_INVALID';
    END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(inventory) WITH ORDINALITY AS entry(value, ordinal) ORDER BY ordinal
    LOOP
        IF jsonb_typeof(item) <> 'object'
           OR (SELECT count(*) FROM jsonb_object_keys(item)) <> 4
           OR NOT (item ?& ARRAY['kind','symbol','signed_quantity','multiplier'])
           OR jsonb_typeof(item->'kind') <> 'string'
           OR item->>'kind' <> 'OPTION'
           OR jsonb_typeof(item->'symbol') <> 'string'
           OR item->>'symbol' !~ '^[A-Z0-9]{1,64}$'
           OR jsonb_typeof(item->'signed_quantity') <> 'string'
           OR item->>'signed_quantity' !~ '^-?[1-9][0-9]*$'
           OR jsonb_typeof(item->'multiplier') <> 'number'
           OR (item->>'multiplier')::numeric <> 100
        THEN RAISE EXCEPTION 'MANAGED_POSITION_FINGERPRINT_INVENTORY_INVALID'; END IF;
        canonical := canonical || separator
          || '{"kind":"OPTION","multiplier":100,"signed_quantity":'
          || to_jsonb(item->>'signed_quantity')::text
          || ',"symbol":' || to_jsonb(item->>'symbol')::text || '}';
        separator := ',';
    END LOOP;
    canonical := canonical || ']';
    RETURN encode(sha256(convert_to(canonical, 'UTF8')), 'hex');
END $$;

CREATE OR REPLACE FUNCTION managed_position_transition_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
                       * CASE leg->>'intent' WHEN 'BUY_TO_OPEN' THEN 1 WHEN 'SELL_TO_OPEN' THEN -1 ELSE 0 END
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

CREATE OR REPLACE FUNCTION managed_position_snapshot_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
       OR NEW.position_fingerprint IS DISTINCT FROM lifecycle_position_fingerprint(expected_inventory) OR NEW.accepted_at IS DISTINCT FROM reconciliation.accepted_at
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
