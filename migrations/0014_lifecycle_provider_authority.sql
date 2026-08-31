ALTER TABLE alpaca_market_sessions
    ADD COLUMN request_hash varchar(64),
    ADD COLUMN retrieved_at timestamptz,
    ADD COLUMN source_payload jsonb;
DROP TRIGGER alpaca_market_session_unavailable_guard ON alpaca_market_sessions;
DROP FUNCTION alpaca_market_session_unavailable_guard();
CREATE FUNCTION alpaca_market_session_provider_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.request_hash !~ '^[0-9a-f]{64}$' OR NEW.retrieved_at IS NULL
       OR NEW.source_payload IS NULL OR NEW.retrieved_at>NEW.created_at
       OR NEW.source_payload->>'market_session_id' IS DISTINCT FROM NEW.market_session_id::text
       OR NEW.source_payload->>'session_date' IS DISTINCT FROM NEW.session_date::text
       OR (NEW.source_payload->>'open_at')::timestamptz IS DISTINCT FROM NEW.open_at
       OR (NEW.source_payload->>'close_at')::timestamptz IS DISTINCT FROM NEW.close_at
       OR NEW.source_payload->>'source_hash' IS DISTINCT FROM NEW.source_hash
       OR NEW.source_payload->>'request_hash' IS DISTINCT FROM NEW.request_hash
       OR (NEW.source_payload->>'retrieved_at')::timestamptz IS DISTINCT FROM NEW.retrieved_at
    THEN RAISE EXCEPTION 'ALPACA_MARKET_SESSION_PROVIDER_AUTHORITY_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER alpaca_market_session_provider_guard BEFORE INSERT ON alpaca_market_sessions
FOR EACH ROW EXECUTE FUNCTION alpaca_market_session_provider_guard();

CREATE TABLE lifecycle_launch_authorities (
    managed_position_id uuid PRIMARY KEY REFERENCES managed_lifecycle_positions(managed_position_id),
    beta60 numeric(18,8) NOT NULL CHECK (beta60>0 AND beta60<=3),
    benchmark_symbol varchar(6) NOT NULL CHECK (benchmark_symbol='QQQ'),
    entry_boundary_at timestamptz NOT NULL,
    entry_policy_hash varchar(64) NOT NULL CHECK (entry_policy_hash ~ '^[0-9a-f]{64}$'),
    underlying_source_hash varchar(64) NOT NULL CHECK (underlying_source_hash ~ '^[0-9a-f]{64}$'),
    benchmark_source_hash varchar(64) NOT NULL CHECK (benchmark_source_hash ~ '^[0-9a-f]{64}$'),
    completed_bar_source_hash varchar(64) NOT NULL CHECK (completed_bar_source_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL
);

CREATE TABLE lifecycle_source_observations (
    source_id uuid PRIMARY KEY,
    external_source_id varchar(128) UNIQUE,
    source_kind varchar(32) NOT NULL CHECK (source_kind IN ('ACCOUNT_SWEEP','OPTION_SNAPSHOT','ATM_IV','UNDERLYING_QUOTE','COMPLETED_BAR','MARKET_CALENDAR','MCP_NEWS','MCP_CORPORATE_ACTION')),
    account_role varchar(16) REFERENCES account_roles(role),
    account_fingerprint varchar(64),
    request_hash varchar(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    result_hash varchar(64) NOT NULL CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    normalized_payload jsonb NOT NULL CHECK (octet_length(normalized_payload::text)<=1048576),
    observed_at timestamptz NOT NULL,
    retrieved_at timestamptz NOT NULL CHECK (retrieved_at>=observed_at),
    source_hash varchar(64) NOT NULL UNIQUE CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL CHECK (created_at>=retrieved_at),
    CHECK ((account_role IS NULL)=(account_fingerprint IS NULL)),
    CHECK (account_role IS NULL OR account_role='DEVELOPMENT')
);

CREATE TABLE lifecycle_account_observations (
    observation_id uuid PRIMARY KEY,
    managed_position_id uuid NOT NULL REFERENCES managed_lifecycle_positions(managed_position_id),
    managed_snapshot_id uuid NOT NULL REFERENCES managed_position_snapshots(snapshot_id),
    account_role varchar(16) NOT NULL REFERENCES account_roles(role) CHECK (account_role='DEVELOPMENT'),
    account_fingerprint varchar(64) NOT NULL CHECK (account_fingerprint ~ '^[0-9a-f]{64}$'),
    sweep_payload jsonb NOT NULL CHECK (jsonb_typeof(sweep_payload)='object' AND octet_length(sweep_payload::text)<=1048576),
    sweep_hash varchar(64) NOT NULL UNIQUE CHECK (sweep_hash ~ '^[0-9a-f]{64}$'),
    retrieval_started_at timestamptz NOT NULL,
    retrieval_completed_at timestamptz NOT NULL CHECK (retrieval_completed_at>=retrieval_started_at),
    accepted_at timestamptz NOT NULL CHECK (accepted_at>=retrieval_completed_at)
);

DROP TRIGGER lifecycle_manifest_guard ON lifecycle_observation_manifests;
DROP FUNCTION lifecycle_manifest_guard();
ALTER TABLE lifecycle_observation_manifests
    ALTER COLUMN agent_input_snapshot_id DROP NOT NULL,
    ALTER COLUMN reconciliation_id DROP NOT NULL,
    ADD COLUMN account_observation_id uuid UNIQUE REFERENCES lifecycle_account_observations(observation_id),
    ADD COLUMN source_authority_manifest jsonb,
    ADD CONSTRAINT ck_lifecycle_manifest_route CHECK (
        (account_observation_id IS NOT NULL AND agent_input_snapshot_id IS NULL AND reconciliation_id IS NULL)
        OR (account_observation_id IS NULL AND agent_input_snapshot_id IS NOT NULL AND reconciliation_id IS NOT NULL)
    );

CREATE TABLE lifecycle_observation_bindings (
    binding_id uuid PRIMARY KEY,
    manifest_id uuid NOT NULL UNIQUE REFERENCES lifecycle_observation_manifests(manifest_id),
    agent_input_snapshot_id uuid NOT NULL UNIQUE REFERENCES agent_input_snapshots(snapshot_id),
    created_at timestamptz NOT NULL
);

CREATE FUNCTION lifecycle_provider_manifest_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE position managed_lifecycle_positions%ROWTYPE; account_observation lifecycle_account_observations%ROWTYPE;
DECLARE greek greek_authority_versions%ROWTYPE;
BEGIN
    IF NEW.account_observation_id IS NULL THEN RETURN NEW; END IF;
    SELECT * INTO position FROM managed_lifecycle_positions WHERE managed_position_id=NEW.managed_position_id;
    SELECT * INTO account_observation FROM lifecycle_account_observations WHERE observation_id=NEW.account_observation_id;
    SELECT * INTO greek FROM greek_authority_versions WHERE authority_id=NEW.greek_authority_id;
    IF position.managed_position_id IS NULL OR position.closed_at IS NOT NULL OR position.account_role<>'DEVELOPMENT'
       OR position.current_snapshot_id IS DISTINCT FROM NEW.managed_snapshot_id
       OR account_observation.managed_position_id IS DISTINCT FROM NEW.managed_position_id
       OR account_observation.managed_snapshot_id IS DISTINCT FROM NEW.managed_snapshot_id
       OR account_observation.account_role IS DISTINCT FROM position.account_role
       OR account_observation.account_fingerprint IS DISTINCT FROM position.account_fingerprint
       OR account_observation.sweep_hash IS DISTINCT FROM NEW.sweep_hash
       OR greek.authority_id IS NULL
       OR jsonb_typeof(NEW.source_authority_manifest) IS DISTINCT FROM 'array'
       OR EXISTS (
           SELECT 1 FROM jsonb_array_elements(NEW.source_authority_manifest) bound
            WHERE NOT EXISTS (
                SELECT 1 FROM lifecycle_source_observations source
                 WHERE source.source_id::text=bound->>'source_id'
                   AND source.external_source_id IS NOT DISTINCT FROM bound->>'external_source_id'
                   AND source.source_kind=bound->>'source_kind'
                   AND source.account_role IS NOT DISTINCT FROM bound->>'account_role'
                   AND source.account_fingerprint IS NOT DISTINCT FROM bound->>'account_fingerprint'
                   AND source.request_hash=bound->>'request_hash'
                   AND source.result_hash=bound->>'result_hash'
                   AND source.normalized_payload=bound->'normalized_payload'
                   AND source.observed_at=(bound->>'observed_at')::timestamptz
                   AND source.retrieved_at=(bound->>'retrieved_at')::timestamptz
                   AND source.source_hash=bound->>'source_hash'
                   AND source.created_at=(bound->>'created_at')::timestamptz
            )
       )
       OR NOT EXISTS (SELECT 1 FROM lifecycle_launch_authorities launch WHERE launch.managed_position_id=position.managed_position_id AND launch.entry_policy_hash=(SELECT policy_hash FROM thesis_versions WHERE thesis_version_id=position.thesis_version_id))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.option_manifest) item WHERE NOT EXISTS (SELECT 1 FROM lifecycle_source_observations source WHERE source.source_hash=item->>'source_hash'))
       OR NOT EXISTS (SELECT 1 FROM lifecycle_source_observations source WHERE source.source_hash=NEW.atm_iv_manifest->>'source_hash' AND source.source_kind='ATM_IV')
       OR NOT EXISTS (SELECT 1 FROM lifecycle_source_observations source WHERE source.source_hash=NEW.atm_iv_manifest->>'call_source_hash' AND source.source_kind='OPTION_SNAPSHOT')
       OR NOT EXISTS (SELECT 1 FROM lifecycle_source_observations source WHERE source.source_hash=NEW.atm_iv_manifest->>'put_source_hash' AND source.source_kind='OPTION_SNAPSHOT')
       OR NOT EXISTS (SELECT 1 FROM lifecycle_source_observations source WHERE source.source_hash=NEW.underlying_manifest->>'quote_source_hash' AND source.source_kind='UNDERLYING_QUOTE')
       OR NOT EXISTS (SELECT 1 FROM lifecycle_source_observations source WHERE source.source_hash=NEW.underlying_manifest->>'completed_bar_source_hash' AND source.source_kind='COMPLETED_BAR')
       OR NOT EXISTS (SELECT 1 FROM lifecycle_source_observations source WHERE source.source_hash=NEW.underlying_manifest->>'benchmark_completed_bar_source_hash' AND source.source_kind='COMPLETED_BAR')
       OR NOT EXISTS (SELECT 1 FROM lifecycle_source_observations source WHERE source.source_hash=NEW.boundary_manifest->'market_session'->>'source_hash' AND source.source_kind='MARKET_CALENDAR')
       OR EXISTS (
           SELECT 1
             FROM jsonb_array_elements(COALESCE(NEW.research_manifest->0->'clusters','[]'::jsonb)) cluster,
                  jsonb_array_elements_text(COALESCE(cluster->'source_ids','[]'::jsonb)) AS source_identifier(value)
            WHERE NOT EXISTS (
                SELECT 1 FROM lifecycle_source_observations source
                 WHERE source.external_source_id=source_identifier.value
                   AND source.source_kind IN ('MCP_NEWS','MCP_CORPORATE_ACTION')
                   AND EXISTS (
                       SELECT 1 FROM jsonb_array_elements(COALESCE(NEW.research_manifest->0->'sources','[]'::jsonb)) bound
                        WHERE bound->>'logical_source_id'=source.external_source_id
                          AND bound->>'source_hash'=source.source_hash
                          AND bound->>'request_hash'=source.request_hash
                          AND bound->>'result_hash'=source.result_hash
                   )
            )
       )
       OR EXISTS (
           SELECT 1 FROM (
               SELECT item->>'source_hash' AS source_hash FROM jsonb_array_elements(NEW.option_manifest) item
               UNION SELECT NEW.atm_iv_manifest->>'source_hash'
               UNION SELECT NEW.atm_iv_manifest->>'call_source_hash'
               UNION SELECT NEW.atm_iv_manifest->>'put_source_hash'
               UNION SELECT NEW.underlying_manifest->>'quote_source_hash'
               UNION SELECT NEW.underlying_manifest->>'completed_bar_source_hash'
               UNION SELECT NEW.underlying_manifest->>'benchmark_completed_bar_source_hash'
               UNION SELECT NEW.boundary_manifest->'market_session'->>'source_hash'
               UNION SELECT point->>'underlying_bar_source_hash' FROM jsonb_array_elements(NEW.boundary_manifest->'price_confirmation') point
               UNION SELECT point->>'benchmark_bar_source_hash' FROM jsonb_array_elements(NEW.boundary_manifest->'price_confirmation') point
               UNION SELECT source.source_hash
                 FROM jsonb_array_elements(COALESCE(NEW.research_manifest->0->'clusters','[]'::jsonb)) cluster,
                      jsonb_array_elements_text(COALESCE(cluster->'source_ids','[]'::jsonb)) AS source_identifier(value)
                 JOIN lifecycle_source_observations source ON source.external_source_id=source_identifier.value
           ) required
           WHERE NOT EXISTS (
               SELECT 1 FROM jsonb_array_elements(NEW.source_authority_manifest) bound
                WHERE bound->>'source_hash'=required.source_hash
           )
       )
    THEN RAISE EXCEPTION 'LIFECYCLE_PROVIDER_MANIFEST_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER lifecycle_provider_manifest_guard AFTER INSERT ON lifecycle_observation_manifests DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lifecycle_provider_manifest_guard();

CREATE FUNCTION lifecycle_observation_binding_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE manifest lifecycle_observation_manifests%ROWTYPE; input agent_input_snapshots%ROWTYPE; position managed_lifecycle_positions%ROWTYPE;
BEGIN
    SELECT * INTO manifest FROM lifecycle_observation_manifests WHERE manifest_id=NEW.manifest_id;
    SELECT * INTO input FROM agent_input_snapshots WHERE snapshot_id=NEW.agent_input_snapshot_id;
    SELECT * INTO position FROM managed_lifecycle_positions WHERE managed_position_id=manifest.managed_position_id;
    IF manifest.account_observation_id IS NULL OR manifest.agent_input_snapshot_id IS NOT NULL OR manifest.reconciliation_id IS NOT NULL
       OR input.decision_kind<>'ASSESSMENT' OR input.account_role<>'DEVELOPMENT'
       OR input.account_role IS DISTINCT FROM position.account_role OR input.account_fingerprint IS DISTINCT FROM position.account_fingerprint
       OR input.thesis_version_id IS DISTINCT FROM position.thesis_version_id
       OR input.normalized_payload->>'acquisition_manifest_id' IS DISTINCT FROM manifest.manifest_id::text
       OR input.normalized_payload->>'acquisition_manifest_hash' IS DISTINCT FROM manifest.manifest_hash
    THEN RAISE EXCEPTION 'LIFECYCLE_OBSERVATION_BINDING_INVALID'; END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER lifecycle_observation_binding_guard AFTER INSERT ON lifecycle_observation_bindings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lifecycle_observation_binding_guard();

CREATE TRIGGER lifecycle_launch_authority_append_only BEFORE UPDATE OR DELETE ON lifecycle_launch_authorities FOR EACH ROW EXECUTE FUNCTION lifecycle_authority_append_only();
CREATE TRIGGER lifecycle_source_observation_append_only BEFORE UPDATE OR DELETE ON lifecycle_source_observations FOR EACH ROW EXECUTE FUNCTION lifecycle_authority_append_only();
CREATE TRIGGER lifecycle_account_observation_append_only BEFORE UPDATE OR DELETE ON lifecycle_account_observations FOR EACH ROW EXECUTE FUNCTION lifecycle_authority_append_only();
CREATE TRIGGER lifecycle_observation_binding_append_only BEFORE UPDATE OR DELETE ON lifecycle_observation_bindings FOR EACH ROW EXECUTE FUNCTION lifecycle_authority_append_only();
