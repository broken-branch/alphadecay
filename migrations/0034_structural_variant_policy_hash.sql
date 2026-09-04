-- Extend the frozen policy identity to the registered structural pilot variants so
-- the database hash matches the policy registry for every registered opportunity key.

CREATE OR REPLACE FUNCTION opportunity_frozen_policy_hash(policy_payload jsonb)
RETURNS varchar(64) IMMUTABLE STRICT LANGUAGE sql AS $$
    SELECT opportunity_evidence_hash(
        'alphadecay.opportunity.policy.v1',
        CASE
            WHEN policy_payload->>'opportunity_key' IN (
                'SPY_STRUCTURAL_BULLISH_BETA_PILOT_V1',
                'SPY_STRUCTURAL_BULLISH_OTM_PILOT_V1',
                'SPY_STRUCTURAL_BEARISH_OTM_PILOT_V1'
            )
            THEN jsonb_build_object(
                'policy', policy_payload,
                'strategy_profile', jsonb_build_object(
                    'direction', CASE
                        WHEN policy_payload->>'opportunity_key' =
                             'SPY_STRUCTURAL_BEARISH_OTM_PILOT_V1'
                        THEN 'BEARISH' ELSE 'BULLISH' END,
                    'target_dte', 38,
                    'minimum_dte', 30,
                    'maximum_dte', 45,
                    'width', '4',
                    'minimum_long_delta', CASE
                        WHEN policy_payload->>'opportunity_key' =
                             'SPY_STRUCTURAL_BULLISH_BETA_PILOT_V1'
                        THEN '0.55' ELSE '0.35' END,
                    'maximum_long_delta', CASE
                        WHEN policy_payload->>'opportunity_key' =
                             'SPY_STRUCTURAL_BULLISH_BETA_PILOT_V1'
                        THEN '0.65' ELSE '0.5' END,
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
