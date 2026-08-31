CREATE TABLE competition_performance_snapshots (
    snapshot_id uuid PRIMARY KEY,
    submission_baseline_id uuid REFERENCES submission_baselines(baseline_id),
    account_role varchar(16) NOT NULL REFERENCES account_roles(role)
        CHECK (account_role = 'SUBMISSION'),
    boundary_key varchar(80) NOT NULL UNIQUE,
    scheduled_for timestamptz NOT NULL UNIQUE,
    attempted_at timestamptz NOT NULL,
    measured_at timestamptz,
    measurement_status varchar(16) NOT NULL
        CHECK (measurement_status IN ('COMPLETE', 'MISSING', 'UNKNOWN')),
    failure_code varchar(40)
        CHECK (failure_code IS NULL OR failure_code IN (
            'CAPTURE_NOT_STARTED', 'PROVIDER_UNAVAILABLE', 'ACCOUNT_STATE_INCOMPLETE',
            'BASELINE_UNAVAILABLE', 'SCHEMA_INVALID'
        )),
    current_equity numeric(18, 6),
    account_equity_change numeric(18, 6),
    account_equity_return_pct numeric(18, 9),
    lifecycle_cashflow numeric(18, 6),
    liquidation_pnl numeric(18, 6),
    baseline_status varchar(32) NOT NULL CHECK (baseline_status IN (
        'BASELINE_NOT_CAPTURED', 'BASELINE_UNKNOWN',
        'BASELINE_CLEAN', 'BASELINE_CONTAMINATED'
    )),
    point_payload jsonb NOT NULL,
    account_fingerprint varchar(64) NOT NULL,
    positions_manifest_hash varchar(64),
    orders_manifest_hash varchar(64),
    activities_manifest_hash varchar(64),
    snapshot_hash varchar(64) NOT NULL UNIQUE,
    CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    CHECK (account_fingerprint ~ '^[0-9a-f]{64}$'),
    CHECK (positions_manifest_hash IS NULL OR positions_manifest_hash ~ '^[0-9a-f]{64}$'),
    CHECK (orders_manifest_hash IS NULL OR orders_manifest_hash ~ '^[0-9a-f]{64}$'),
    CHECK (activities_manifest_hash IS NULL OR activities_manifest_hash ~ '^[0-9a-f]{64}$'),
    CHECK (
        (scheduled_for = timestamptz '2026-09-04 14:30:00+00')
        = (boundary_key = 'FINAL_2026-09-04T14:30:00Z')
    ),
    CHECK (attempted_at >= scheduled_for),
    CHECK (measured_at IS NULL OR measured_at >= attempted_at),
    CHECK (
        (measurement_status = 'COMPLETE'
            AND failure_code IS NULL
            AND measured_at IS NOT NULL
            AND current_equity IS NOT NULL)
        OR
        (measurement_status <> 'COMPLETE'
            AND failure_code IS NOT NULL
            AND measured_at IS NULL
            AND current_equity IS NULL
            AND account_equity_change IS NULL
            AND account_equity_return_pct IS NULL
            AND lifecycle_cashflow IS NULL
            AND liquidation_pnl IS NULL)
    ),
    CHECK (
        (baseline_status = 'BASELINE_CLEAN' AND measurement_status = 'COMPLETE'
            AND account_equity_change = current_equity - 100000
            AND account_equity_return_pct * 1000 = account_equity_change)
        OR
        ((baseline_status <> 'BASELINE_CLEAN' OR measurement_status <> 'COMPLETE')
            AND account_equity_change IS NULL
            AND account_equity_return_pct IS NULL)
    ),
    CHECK (
        (measurement_status = 'COMPLETE'
            AND positions_manifest_hash IS NOT NULL
            AND orders_manifest_hash IS NOT NULL
            AND activities_manifest_hash IS NOT NULL)
        OR
        (measurement_status <> 'COMPLETE'
            AND positions_manifest_hash IS NULL
            AND orders_manifest_hash IS NULL
            AND activities_manifest_hash IS NULL)
    ),
    CHECK (
        (submission_baseline_id IS NULL
            AND baseline_status = 'BASELINE_NOT_CAPTURED'
            AND measurement_status <> 'COMPLETE')
        OR
        (submission_baseline_id IS NOT NULL
            AND baseline_status IN ('BASELINE_CLEAN', 'BASELINE_CONTAMINATED'))
    )
);

CREATE INDEX ix_competition_performance_scheduled
    ON competition_performance_snapshots (scheduled_for);

CREATE OR REPLACE FUNCTION lock_competition_performance_snapshot_insert()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    account account_roles%%ROWTYPE;
    baseline submission_baselines%%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(6748832220);
    SELECT * INTO account FROM account_roles WHERE role = 'SUBMISSION';
    IF NOT FOUND OR NEW.account_role <> 'SUBMISSION'
        OR NEW.account_fingerprint <> account.account_fingerprint
    THEN
        RAISE EXCEPTION 'performance snapshot account binding mismatch';
    END IF;
    IF NEW.submission_baseline_id IS NULL THEN
        IF NEW.baseline_status <> 'BASELINE_NOT_CAPTURED'
            OR NEW.measurement_status = 'COMPLETE'
        THEN
            RAISE EXCEPTION 'performance snapshot baseline binding mismatch';
        END IF;
    ELSE
        SELECT * INTO baseline
        FROM submission_baselines
        WHERE baseline_id = NEW.submission_baseline_id;
        IF NOT FOUND OR baseline.account_role <> 'SUBMISSION'
            OR baseline.account_fingerprint <> account.account_fingerprint
            OR NEW.baseline_status <> (CASE WHEN baseline.contaminated
                THEN 'BASELINE_CONTAMINATED' ELSE 'BASELINE_CLEAN' END)
        THEN
            RAISE EXCEPTION 'performance snapshot baseline binding mismatch';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER competition_performance_snapshot_insert_guard
BEFORE INSERT ON competition_performance_snapshots
FOR EACH ROW EXECUTE FUNCTION lock_competition_performance_snapshot_insert();

CREATE TABLE competition_performance_publications (
    publication_id uuid PRIMARY KEY,
    snapshot_id uuid NOT NULL UNIQUE
        REFERENCES competition_performance_snapshots(snapshot_id),
    boundary_scheduled_for timestamptz NOT NULL UNIQUE,
    published_at timestamptz NOT NULL,
    payload_text text NOT NULL,
    projection_hash varchar(64) NOT NULL,
    publication_hash varchar(64) NOT NULL UNIQUE,
    predecessor_hash varchar(64),
    CHECK (published_at >= boundary_scheduled_for),
    CHECK (projection_hash ~ '^[0-9a-f]{64}$'),
    CHECK (publication_hash ~ '^[0-9a-f]{64}$'),
    CHECK (predecessor_hash IS NULL OR predecessor_hash ~ '^[0-9a-f]{64}$')
);

CREATE UNIQUE INDEX uq_competition_performance_predecessor
    ON competition_performance_publications (predecessor_hash)
    WHERE predecessor_hash IS NOT NULL;
CREATE INDEX ix_competition_performance_published
    ON competition_performance_publications (published_at);

CREATE OR REPLACE FUNCTION enforce_competition_performance_publication_insert()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    prior competition_performance_publications%%ROWTYPE;
    source competition_performance_snapshots%%ROWTYPE;
    payload jsonb;
BEGIN
    PERFORM pg_advisory_xact_lock(6748832220);
    SELECT * INTO source
    FROM competition_performance_snapshots
    WHERE snapshot_id = NEW.snapshot_id;
    IF NOT FOUND OR source.scheduled_for <> NEW.boundary_scheduled_for THEN
        RAISE EXCEPTION 'performance publication snapshot boundary mismatch';
    END IF;

    BEGIN
        payload := NEW.payload_text::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'performance publication payload is not JSON';
    END;
    IF (SELECT count(*) FROM jsonb_object_keys(payload)) <> 8
        OR payload ->> 'schema_version' IS DISTINCT FROM 'v1'
        OR payload ->> 'publication_status' IS DISTINCT FROM 'PUBLISHED'
        OR payload -> 'point' IS DISTINCT FROM source.point_payload
        OR payload ->> 'baseline_status' IS DISTINCT FROM source.baseline_status
        OR (payload ->> 'published_at')::timestamptz IS DISTINCT FROM NEW.published_at
        OR payload -> 'linked_certificate_ids' IS DISTINCT FROM '[]'::jsonb
        OR payload ->> 'publication_hash' IS DISTINCT FROM NEW.publication_hash
        OR (payload ->> 'predecessor_hash') IS DISTINCT FROM NEW.predecessor_hash
    THEN
        RAISE EXCEPTION 'performance publication payload does not match stored columns';
    END IF;

    SELECT * INTO prior
    FROM competition_performance_publications
    ORDER BY boundary_scheduled_for DESC
    LIMIT 1;
    IF FOUND THEN
        IF NEW.boundary_scheduled_for <= prior.boundary_scheduled_for
            OR NEW.predecessor_hash IS DISTINCT FROM prior.publication_hash
        THEN
            RAISE EXCEPTION 'performance publication chain is not monotonic';
        END IF;
    ELSIF NEW.predecessor_hash IS NOT NULL THEN
        RAISE EXCEPTION 'genesis performance publication has a predecessor';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER competition_performance_publication_insert_guard
BEFORE INSERT ON competition_performance_publications
FOR EACH ROW EXECUTE FUNCTION enforce_competition_performance_publication_insert();

CREATE OR REPLACE FUNCTION reject_competition_performance_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    RAISE EXCEPTION 'competition performance records are append-only';
END
$function$;

CREATE TRIGGER competition_performance_snapshots_append_only
BEFORE UPDATE OR DELETE ON competition_performance_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_competition_performance_mutation();

CREATE TRIGGER competition_performance_publications_append_only
BEFORE UPDATE OR DELETE ON competition_performance_publications
FOR EACH ROW EXECUTE FUNCTION reject_competition_performance_mutation();
