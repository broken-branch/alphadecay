-- Allow re-entry on the same event key within a trading day. The one-entry-per-event-day
-- rule was lifted from the entry policy on 2026-09-03 (operator decision); the lifetime
-- entry count, lifetime risk, and per-position risk caps remain the binding limits.
-- The partial unique index becomes a plain lookup index.

DROP INDEX IF EXISTS uq_entry_event_day;

CREATE INDEX IF NOT EXISTS ix_entry_event_day
    ON execution_intents (account_role, event_key, trading_day)
    WHERE action = 'ENTRY';
