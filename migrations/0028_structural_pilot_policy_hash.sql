-- Keep the exact structural pilot profile inside the frozen policy identity while
-- retaining the policy payload shape consumed by the opportunity runtime.

CREATE FUNCTION opportunity_frozen_policy_hash(policy_payload jsonb)
RETURNS varchar(64) IMMUTABLE STRICT LANGUAGE sql AS $$
    SELECT opportunity_evidence_hash(
        'alphadecay.opportunity.policy.v1',
        CASE
            WHEN policy_payload->>'opportunity_key' =
                 'SPY_STRUCTURAL_BULLISH_BETA_PILOT_V1'
            THEN jsonb_build_object(
                'policy', policy_payload,
                'strategy_profile', jsonb_build_object(
                    'direction', 'BULLISH',
                    'target_dte', 38,
                    'minimum_dte', 30,
                    'maximum_dte', 45,
                    'width', '4',
                    'minimum_long_delta', '0.55',
                    'maximum_long_delta', '0.65',
                    'quantity', 1,
                    'maximum_debit', '2.25',
                    'minimum_reward_to_risk', '0.75',
                    'maximum_relative_spread', '0.05',
                    'maximum_quote_age', jsonb_build_object(
                        'days', 0, 'seconds', 20, 'microseconds', 0
                    ),
                    'maximum_quote_skew', jsonb_build_object(
                        'days', 0, 'seconds', 3, 'microseconds', 0
                    )
                )
            )
            ELSE policy_payload
        END
    )
$$;

DO $migration$
DECLARE
    definition text;
    rewritten text;
BEGIN
    SELECT pg_get_functiondef('development_opportunity_plan_guard()'::regprocedure)
      INTO definition;
    rewritten := replace(
        definition,
        'opportunity_evidence_hash(
              ''alphadecay.opportunity.policy.v1'', NEW.policy_payload)',
        'opportunity_frozen_policy_hash(NEW.policy_payload)'
    );
    IF rewritten IS NOT DISTINCT FROM definition THEN
        RAISE EXCEPTION 'STRUCTURAL_POLICY_HASH_GUARD_FRAGMENT_MISSING';
    END IF;
    EXECUTE rewritten;
END
$migration$;

CREATE FUNCTION opportunity_final_reconciliation_hash(
    intent_digest text,
    attempt_ordinal integer,
    observation_hash text
) RETURNS varchar(64) IMMUTABLE STRICT LANGUAGE sql AS $$
    SELECT encode(
        sha256(convert_to(opportunity_canonical_json(jsonb_build_object(
            'domain', 'alphadecay.final-reconciliation.v1',
            'intent_digest', intent_digest,
            'attempt_ordinal', attempt_ordinal,
            'last_observation_hash', observation_hash
        )), 'UTF8')),
        'hex'
    )
$$;

DO $migration$
DECLARE
    definition text;
    rewritten text;
BEGIN
    SELECT pg_get_functiondef('managed_position_snapshot_guard()'::regprocedure)
      INTO definition;
    rewritten := replace(
        definition,
        'AND permit.request_hash=reconciliation.request_hash',
        'AND reconciliation.request_hash=opportunity_final_reconciliation_hash(
                    permit.intent_digest, permit.attempt_ordinal,
                    observation.observation_hash)'
    );
    rewritten := replace(
        rewritten,
        'AND permit.request_hash=reconciliation.expectation_payload->>''request_hash''',
        'AND reconciliation.request_hash=
                    reconciliation.expectation_payload->>''request_hash'''
    );
    rewritten := replace(
        rewritten,
        'AND permit.reconciliation_id=reconciliation.reconciliation_id',
        ''
    );
    IF rewritten IS NOT DISTINCT FROM definition THEN
        RAISE EXCEPTION 'FINAL_RECONCILIATION_GUARD_FRAGMENT_MISSING';
    END IF;
    EXECUTE rewritten;
END
$migration$;
