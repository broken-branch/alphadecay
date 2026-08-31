DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM execution_intents WHERE action = 'ROLL') THEN
        RAISE EXCEPTION 'existing roll intents cannot acquire frozen lifecycle authority';
    END IF;
END
$$;

ALTER TABLE execution_intents
    ADD COLUMN market_session_id uuid,
    ADD COLUMN quoted_relative_spread numeric(18,10),
    ADD COLUMN maximum_relative_spread numeric(18,10),
    ADD COLUMN incremental_debit numeric(18,6),
    ADD COLUMN maximum_incremental_debit numeric(18,6),
    ADD CONSTRAINT ck_intent_roll_authority CHECK (
        (action = 'ROLL'
         AND market_session_id IS NOT NULL
         AND quoted_relative_spread IS NOT NULL
         AND quoted_relative_spread >= 0
         AND maximum_relative_spread IS NOT NULL
         AND maximum_relative_spread >= quoted_relative_spread
         AND maximum_relative_spread < 1
         AND incremental_debit IS NOT NULL
         AND incremental_debit >= 0
         AND maximum_incremental_debit IS NOT NULL
         AND maximum_incremental_debit >= incremental_debit
         AND maximum_incremental_debit <= approved_max_loss)
        OR
        (action <> 'ROLL'
         AND market_session_id IS NULL
         AND quoted_relative_spread IS NULL
         AND maximum_relative_spread IS NULL
         AND incremental_debit IS NULL
         AND maximum_incremental_debit IS NULL)
    );

CREATE UNIQUE INDEX uq_roll_intent_per_position_session
    ON execution_intents(account_role, fingerprint, market_session_id)
    WHERE action = 'ROLL';
