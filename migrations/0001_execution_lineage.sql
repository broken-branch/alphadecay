CREATE TABLE account_roles (
    role varchar(16) PRIMARY KEY CHECK (role IN ('SUBMISSION', 'DEVELOPMENT')),
    account_fingerprint varchar(64) NOT NULL UNIQUE,
    equity numeric(18, 6) NOT NULL,
    autonomous_enabled boolean NOT NULL DEFAULT false
);

CREATE TABLE submission_baselines (
    baseline_id uuid PRIMARY KEY,
    account_role varchar(16) NOT NULL UNIQUE REFERENCES account_roles(role),
    account_fingerprint varchar(64) NOT NULL,
    equity numeric(18, 6) NOT NULL CHECK (equity = 100000),
    captured_at timestamptz NOT NULL,
    positions_hash varchar(64) NOT NULL,
    orders_hash varchar(64) NOT NULL,
    activities_hash varchar(64) NOT NULL,
    contaminated boolean NOT NULL DEFAULT false
);

CREATE TABLE entry_approval_certificates (
    approval_id uuid PRIMARY KEY,
    account_role varchar(16) NOT NULL REFERENCES account_roles(role),
    policy_hash varchar(64) NOT NULL,
    book_fingerprint varchar(64) NOT NULL,
    envelope_hash varchar(64) NOT NULL,
    approved_max_loss numeric(18, 6) NOT NULL CHECK (approved_max_loss > 0),
    quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 100),
    valid_from timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    valid boolean NOT NULL DEFAULT true,
    CHECK (expires_at > valid_from)
);

CREATE TABLE assessment_certificates (
    certificate_id uuid PRIMARY KEY,
    assessment_id uuid NOT NULL UNIQUE,
    account_role varchar(16) NOT NULL REFERENCES account_roles(role),
    action varchar(8) NOT NULL CHECK (action IN ('CLOSE', 'ROLL')),
    position_fingerprint varchar(64) NOT NULL,
    envelope_hash varchar(64) NOT NULL,
    approved_max_loss numeric(18, 6) NOT NULL CHECK (approved_max_loss > 0),
    quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 100),
    expected_after_exposure jsonb,
    policy_hash varchar(64) NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    valid boolean NOT NULL DEFAULT true,
    CHECK (expires_at > created_at)
);

CREATE TABLE execution_intents (
    intent_id uuid PRIMARY KEY,
    account_role varchar(16) NOT NULL REFERENCES account_roles(role),
    intent_digest varchar(64) NOT NULL UNIQUE,
    action varchar(8) NOT NULL CHECK (action IN ('ENTRY', 'CLOSE', 'ROLL')),
    policy_hash varchar(64) NOT NULL,
    event_key varchar(80) NOT NULL,
    trading_day date NOT NULL,
    entry_approval_id uuid REFERENCES entry_approval_certificates(approval_id),
    assessment_certificate_id uuid REFERENCES assessment_certificates(certificate_id),
    fingerprint varchar(64) NOT NULL,
    envelope_hash varchar(64) NOT NULL,
    envelope_payload jsonb NOT NULL,
    legs jsonb NOT NULL,
    quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 100),
    minimum_limit numeric(18, 6) NOT NULL,
    maximum_limit numeric(18, 6) NOT NULL,
    approved_max_loss numeric(18, 6) NOT NULL
        CHECK (approved_max_loss > 0 AND approved_max_loss <= 100000),
    state varchar(24) NOT NULL CHECK (state IN ('APPROVED', 'CLAIMED', 'TERMINAL')),
    claimed_by varchar(16),
    claimed_at timestamptz,
    first_fill_consumed boolean NOT NULL DEFAULT false,
    CHECK (minimum_limit <= maximum_limit),
    CHECK (
        (action = 'ENTRY' AND entry_approval_id IS NOT NULL
            AND assessment_certificate_id IS NULL)
        OR
        (action IN ('CLOSE', 'ROLL') AND entry_approval_id IS NULL
            AND assessment_certificate_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_entry_event_day
    ON execution_intents (account_role, event_key, trading_day)
    WHERE action = 'ENTRY';
CREATE UNIQUE INDEX uq_active_intent_per_account
    ON execution_intents (account_role)
    WHERE action = 'ENTRY' AND state <> 'TERMINAL';

CREATE TABLE order_attempts (
    attempt_id uuid PRIMARY KEY,
    execution_intent_id uuid NOT NULL REFERENCES execution_intents(intent_id),
    attempt_ordinal integer NOT NULL CHECK (attempt_ordinal BETWEEN 0 AND 3),
    client_order_id varchar(64) NOT NULL UNIQUE,
    provider_order_id varchar(128) UNIQUE,
    state varchar(40) NOT NULL,
    request_hash varchar(64) NOT NULL,
    replaces_attempt_id uuid REFERENCES order_attempts(attempt_id),
    filled_quantity integer NOT NULL DEFAULT 0,
    quantity integer NOT NULL DEFAULT 0,
    CHECK (quantity BETWEEN 0 AND 100),
    CHECK (filled_quantity BETWEEN 0 AND quantity),
    UNIQUE (execution_intent_id, attempt_ordinal)
);

CREATE TABLE execution_certificates (
    certificate_id uuid PRIMARY KEY,
    execution_intent_id uuid NOT NULL UNIQUE REFERENCES execution_intents(intent_id),
    entry_approval_id uuid REFERENCES entry_approval_certificates(approval_id),
    assessment_certificate_id uuid REFERENCES assessment_certificates(certificate_id),
    execution_status varchar(48) NOT NULL,
    attempt_ids jsonb NOT NULL,
    actual_exposure jsonb,
    reconciliation_checks jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    CHECK ((entry_approval_id IS NULL) <> (assessment_certificate_id IS NULL))
);

CREATE TABLE competition_entry_budget (
    account_role varchar(16) PRIMARY KEY REFERENCES account_roles(role),
    entries_used integer NOT NULL DEFAULT 0 CHECK (entries_used BETWEEN 0 AND 1000),
    gross_approved_risk numeric(18, 6) NOT NULL DEFAULT 0
        CHECK (gross_approved_risk BETWEEN 0 AND 100000),
    reserved_intent_id uuid REFERENCES execution_intents(intent_id),
    reserved_risk numeric(18, 6) NOT NULL DEFAULT 0 CHECK (reserved_risk >= 0)
);
