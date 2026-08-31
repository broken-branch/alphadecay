-- Keep terminal execution results aligned with the broker order-state model.

DO $migration$
DECLARE status_constraint text;
BEGIN
    SELECT constraint_row.conname INTO status_constraint
    FROM pg_constraint AS constraint_row
    WHERE constraint_row.conrelid = 'entry_materialization_jobs'::regclass
      AND constraint_row.contype = 'c'
      AND pg_get_constraintdef(constraint_row.oid)
          LIKE '%PARTIAL_CANCELED_RECONCILED%';

    IF status_constraint IS NULL THEN
        RAISE EXCEPTION 'ENTRY_MATERIALIZATION_STATUS_CONSTRAINT_MISSING';
    END IF;

    EXECUTE format(
        'ALTER TABLE entry_materialization_jobs DROP CONSTRAINT %I',
        status_constraint
    );
END
$migration$;

ALTER TABLE entry_materialization_jobs
    ADD CONSTRAINT ck_entry_materialization_terminal_status CHECK (
        terminal_status IN (
            'FILLED',
            'REJECTED',
            'CANCELED',
            'EXPIRED',
            'REPLACED'
        )
    );

DO $migration$
DECLARE
    source_constraint text;
    matching_constraints integer;
BEGIN
    SELECT count(*), min(constraint_row.conname)
    INTO matching_constraints, source_constraint
    FROM pg_constraint AS constraint_row
    WHERE constraint_row.conrelid = 'attempt_observations'::regclass
      AND constraint_row.contype = 'c'
      AND pg_get_constraintdef(constraint_row.oid) LIKE '%DISPATCH_OUTCOME%'
      AND pg_get_constraintdef(constraint_row.oid) LIKE '%TARGETED_LOOKUP%';

    IF matching_constraints <> 1 OR source_constraint IS NULL THEN
        RAISE EXCEPTION 'ATTEMPT_OBSERVATION_SOURCE_CONSTRAINT_INVALID';
    END IF;

    EXECUTE format(
        'ALTER TABLE attempt_observations DROP CONSTRAINT %I',
        source_constraint
    );
END
$migration$;

ALTER TABLE attempt_observations
    ADD CONSTRAINT ck_attempt_observation_source CHECK (
        source IN ('DISPATCH_OUTCOME', 'TARGETED_LOOKUP', 'TARGETED_LOOKUP_FAILURE')
    );

ALTER TABLE account_roles DROP CONSTRAINT ck_account_execution_lock;
ALTER TABLE account_roles
    ADD CONSTRAINT ck_account_execution_lock CHECK (
        (execution_locked = false AND execution_lock_reason IS NULL
         AND execution_locked_at IS NULL AND execution_lock_id IS NULL
         AND recovery_pending = false)
        OR
        (execution_locked = true AND execution_lock_reason IN (
            'ASSIGNMENT_SUSPECTED', 'RECONCILIATION_MISMATCH',
            'ENTRY_EQUITY_FLOOR', 'ENTRY_OPEN_POSITION_LIMIT',
            'ENTRY_LIMITS_REQUIRED', 'ENTRY_POLICY_AUTHORITY_MISMATCH',
            'ENTRY_COUNT_EXHAUSTED', 'ENTRY_POSITION_RISK_EXHAUSTED',
            'ENTRY_RISK_EXHAUSTED', 'ENTRY_QUANTITY_EXHAUSTED',
            'BROKER_TRANSITION_STALLED', 'UNMANAGED_PARTIAL_EXPOSURE'
        ) AND execution_locked_at IS NOT NULL AND execution_lock_id IS NOT NULL
          AND execution_lock_generation > 0)
    );

DO $migration$
DECLARE
    guard_definition text;
    updated_definition text;
BEGIN
    SELECT pg_get_functiondef(routine.oid) INTO guard_definition
    FROM pg_proc AS routine
    WHERE routine.proname = 'guard_agent_tick_transition'
      AND routine.pronamespace = current_schema()::regnamespace;

    IF guard_definition IS NULL THEN
        RAISE EXCEPTION 'AGENT_TICK_TRANSITION_GUARD_MISSING';
    END IF;

    updated_definition := replace(
        guard_definition,
        '''PARTIAL_CANCELED_RECONCILED'',',
        '''REPLACED'', ''PARTIAL_CANCELED_RECONCILED'',
            ''PARTIAL_EXPIRED_RECONCILED'',
            ''PARTIAL_REPLACED_RECONCILED'','
    );
    IF updated_definition = guard_definition THEN
        RAISE EXCEPTION 'AGENT_TICK_TERMINAL_STATUS_LIST_NOT_FOUND';
    END IF;
    EXECUTE updated_definition;
END
$migration$;
