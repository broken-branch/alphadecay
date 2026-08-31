ALTER TABLE account_roles
    ADD COLUMN IF NOT EXISTS execution_locked boolean NOT NULL DEFAULT false;
ALTER TABLE account_roles
    ADD COLUMN IF NOT EXISTS execution_lock_reason varchar(40);
ALTER TABLE account_roles
    ADD COLUMN IF NOT EXISTS execution_locked_at timestamptz;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_account_execution_lock'
          AND conrelid = 'account_roles'::regclass
    ) THEN
        ALTER TABLE account_roles
            ADD CONSTRAINT ck_account_execution_lock
            CHECK (
                (execution_locked = false AND execution_lock_reason IS NULL
                    AND execution_locked_at IS NULL)
                OR
                (execution_locked = true AND execution_lock_reason IN
                    ('ASSIGNMENT_SUSPECTED', 'RECONCILIATION_MISMATCH')
                    AND execution_locked_at IS NOT NULL)
            );
    END IF;
END
$migration$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_claimed_execution_lease
    ON execution_intents (account_role)
    WHERE state = 'CLAIMED';
