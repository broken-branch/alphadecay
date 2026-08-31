CREATE TABLE competition_record_publications (
    publication_id uuid PRIMARY KEY,
    record_kind varchar(16) NOT NULL CHECK (record_kind IN ('NO_TRADE', 'POSITION')),
    account_role varchar(16) NOT NULL REFERENCES account_roles(role)
        CHECK (account_role = 'SUBMISSION'),
    source_decision_id uuid UNIQUE REFERENCES agent_decisions(decision_id),
    source_managed_position_id uuid REFERENCES managed_lifecycle_positions(managed_position_id),
    public_record_id varchar(64) NOT NULL CHECK (public_record_id ~ '^[0-9a-f]{64}$'),
    source_authority_hash varchar(64) NOT NULL UNIQUE
        CHECK (source_authority_hash ~ '^[0-9a-f]{64}$'),
    occurred_at timestamptz NOT NULL,
    published_at timestamptz NOT NULL UNIQUE,
    payload_text text NOT NULL,
    projection_hash varchar(64) NOT NULL CHECK (projection_hash ~ '^[0-9a-f]{64}$'),
    publication_hash varchar(64) NOT NULL UNIQUE
        CHECK (publication_hash ~ '^[0-9a-f]{64}$'),
    predecessor_hash varchar(64)
        CHECK (predecessor_hash IS NULL OR predecessor_hash ~ '^[0-9a-f]{64}$'),
    UNIQUE (public_record_id, source_authority_hash),
    CHECK (
        (record_kind = 'NO_TRADE'
            AND source_decision_id IS NOT NULL
            AND source_managed_position_id IS NULL)
        OR
        (record_kind = 'POSITION'
            AND source_decision_id IS NULL
            AND source_managed_position_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_competition_record_predecessor
    ON competition_record_publications (predecessor_hash)
    WHERE predecessor_hash IS NOT NULL;
CREATE INDEX ix_competition_record_occurred
    ON competition_record_publications (occurred_at);
CREATE INDEX ix_competition_record_published
    ON competition_record_publications (published_at);

CREATE FUNCTION competition_record_utc_text(value timestamptz)
RETURNS text IMMUTABLE STRICT LANGUAGE sql AS $function$
    SELECT to_char(value AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
        || CASE WHEN extract(microseconds FROM value)::bigint %% 1000000 = 0
                THEN ''
                ELSE '.' || to_char(value AT TIME ZONE 'UTC', 'US')
           END
        || 'Z'
$function$;

CREATE FUNCTION competition_record_public_id(kind text, stable_hash text)
RETURNS varchar(64) IMMUTABLE STRICT LANGUAGE sql AS $function$
    SELECT opportunity_plain_hash(jsonb_build_object(
        'domain', 'alphadecay.competition-record-public-id.v1',
        'kind', kind,
        'source', stable_hash
    ))
$function$;

CREATE FUNCTION competition_record_spread(inventory jsonb)
RETURNS jsonb IMMUTABLE STRICT LANGUAGE plpgsql AS $function$
DECLARE
    item jsonb;
    symbol_match text[];
    common_underlying text;
    common_expiry text;
    common_right text;
    long_strike numeric;
    short_strike numeric;
    common_quantity integer;
    item_quantity integer;
    long_count integer := 0;
    short_count integer := 0;
BEGIN
    IF jsonb_typeof(inventory) <> 'array' OR jsonb_array_length(inventory) <> 2 THEN
        RAISE EXCEPTION 'competition position is not a two-leg vertical';
    END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(inventory) AS entry(value)
    LOOP
        IF jsonb_typeof(item) <> 'object'
            OR (SELECT count(*) FROM jsonb_object_keys(item)) <> 4
            OR NOT (item ?& ARRAY['kind','symbol','signed_quantity','multiplier'])
            OR item->>'kind' <> 'OPTION'
            OR jsonb_typeof(item->'symbol') <> 'string'
            OR jsonb_typeof(item->'signed_quantity') <> 'string'
            OR item->>'signed_quantity' !~ '^-?[1-9][0-9]*$'
            OR jsonb_typeof(item->'multiplier') <> 'number'
            OR (item->>'multiplier')::numeric <> 100
        THEN
            RAISE EXCEPTION 'competition position inventory is invalid';
        END IF;
        symbol_match := regexp_match(
            item->>'symbol', '^([A-Z]{1,6})([0-9]{6})([CP])([0-9]{8})$'
        );
        IF symbol_match IS NULL THEN
            RAISE EXCEPTION 'competition option symbol is invalid';
        END IF;
        IF common_underlying IS NULL THEN
            common_underlying := symbol_match[1];
            common_expiry := symbol_match[2];
            common_right := symbol_match[3];
            common_quantity := abs((item->>'signed_quantity')::integer);
        ELSIF common_underlying IS DISTINCT FROM symbol_match[1]
            OR common_expiry IS DISTINCT FROM symbol_match[2]
            OR common_right IS DISTINCT FROM symbol_match[3]
            OR common_quantity IS DISTINCT FROM abs((item->>'signed_quantity')::integer)
        THEN
            RAISE EXCEPTION 'competition option legs are not one vertical';
        END IF;
        item_quantity := (item->>'signed_quantity')::integer;
        IF item_quantity > 0 THEN
            long_count := long_count + 1;
            long_strike := symbol_match[4]::numeric / 1000;
        ELSE
            short_count := short_count + 1;
            short_strike := symbol_match[4]::numeric / 1000;
        END IF;
    END LOOP;
    IF long_count <> 1 OR short_count <> 1 OR long_strike = short_strike THEN
        RAISE EXCEPTION 'competition option sides are not a vertical';
    END IF;
    IF to_char(to_date(common_expiry, 'YYMMDD'), 'YYMMDD') <> common_expiry THEN
        RAISE EXCEPTION 'competition option expiry is invalid';
    END IF;
    RETURN jsonb_build_object(
        'structure', 'VERTICAL',
        'underlying', common_underlying,
        'option_type', CASE common_right WHEN 'C' THEN 'CALL' ELSE 'PUT' END,
        'expiration', to_char(to_date(common_expiry, 'YYMMDD'), 'YYYY-MM-DD'),
        'long_strike', trim_scale(long_strike)::text,
        'short_strike', trim_scale(short_strike)::text,
        'quantity', common_quantity
    );
END
$function$;

CREATE FUNCTION competition_record_exposure(value jsonb)
RETURNS jsonb IMMUTABLE LANGUAGE plpgsql AS $function$
DECLARE
    key text;
    present integer := 0;
BEGIN
    IF value IS NULL OR value = 'null'::jsonb THEN RETURN NULL; END IF;
    IF jsonb_typeof(value) <> 'object'
       OR EXISTS (
            SELECT 1 FROM jsonb_object_keys(value) AS item(key)
            WHERE item.key NOT IN ('delta','gamma','theta_per_day','vega_per_iv_point')
       )
    THEN RAISE EXCEPTION 'competition exposure projection is invalid'; END IF;
    FOREACH key IN ARRAY ARRAY['delta','gamma','theta_per_day','vega_per_iv_point']
    LOOP
        IF value ? key THEN
            present := present + 1;
            IF jsonb_typeof(value->key) <> 'string'
                OR value->>key !~ '^-?[0-9]+(\.[0-9]+)?$'
            THEN RAISE EXCEPTION 'competition exposure value is invalid'; END IF;
        END IF;
    END LOOP;
    IF present = 0 THEN
        RAISE EXCEPTION 'competition exposure projection is empty';
    END IF;
    RETURN jsonb_build_object(
        'delta', CASE WHEN value ? 'delta' THEN value->>'delta' ELSE NULL END,
        'gamma', CASE WHEN value ? 'gamma' THEN value->>'gamma' ELSE NULL END,
        'theta_per_day', CASE WHEN value ? 'theta_per_day'
            THEN value->>'theta_per_day' ELSE NULL END,
        'vega_per_iv_point', CASE WHEN value ? 'vega_per_iv_point'
            THEN value->>'vega_per_iv_point' ELSE NULL END
    );
END
$function$;

CREATE FUNCTION competition_record_intent_matches_snapshot(
    source_intent_id uuid,
    source_action text,
    source_inventory jsonb,
    predecessor_inventory jsonb
) RETURNS boolean STABLE LANGUAGE plpgsql AS $function$
DECLARE
    intent execution_intents%%ROWTYPE;
    leg jsonb;
    opening_inventory jsonb := '[]'::jsonb;
    closing_inventory jsonb := '[]'::jsonb;
    opening_count integer := 0;
    closing_count integer := 0;
    buy_open integer := 0;
    sell_open integer := 0;
    buy_close integer := 0;
    sell_close integer := 0;
BEGIN
    SELECT * INTO intent FROM execution_intents
      WHERE execution_intents.intent_id = source_intent_id;
    IF intent.intent_id IS NULL OR intent.account_role <> 'SUBMISSION'
       OR intent.state <> 'TERMINAL' OR intent.action IS DISTINCT FROM source_action
       OR intent.quantity <= 0 OR jsonb_typeof(intent.legs) <> 'array'
       OR jsonb_array_length(intent.legs) <> (CASE source_action WHEN 'ROLL' THEN 4 ELSE 2 END)
    THEN RAISE EXCEPTION 'competition position intent is invalid'; END IF;
    FOR leg IN SELECT value FROM jsonb_array_elements(intent.legs) AS item(value)
    LOOP
        IF jsonb_typeof(leg) <> 'object'
           OR NOT (leg ?& ARRAY['symbol','intent','ratio'])
           OR leg->>'symbol' !~ '^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$'
           OR leg->>'ratio' <> '1'
        THEN RAISE EXCEPTION 'competition position intent leg is invalid'; END IF;
        CASE leg->>'intent'
            WHEN 'BUY_TO_OPEN' THEN
                buy_open := buy_open + 1; opening_count := opening_count + 1;
                opening_inventory := opening_inventory || jsonb_build_array(jsonb_build_object(
                    'kind','OPTION','symbol',leg->>'symbol',
                    'signed_quantity',intent.quantity::text,'multiplier',100));
            WHEN 'SELL_TO_OPEN' THEN
                sell_open := sell_open + 1; opening_count := opening_count + 1;
                opening_inventory := opening_inventory || jsonb_build_array(jsonb_build_object(
                    'kind','OPTION','symbol',leg->>'symbol',
                    'signed_quantity',(-intent.quantity)::text,'multiplier',100));
            WHEN 'BUY_TO_CLOSE' THEN
                buy_close := buy_close + 1; closing_count := closing_count + 1;
                closing_inventory := closing_inventory || jsonb_build_array(jsonb_build_object(
                    'kind','OPTION','symbol',leg->>'symbol',
                    'signed_quantity',(-intent.quantity)::text,'multiplier',100));
            WHEN 'SELL_TO_CLOSE' THEN
                sell_close := sell_close + 1; closing_count := closing_count + 1;
                closing_inventory := closing_inventory || jsonb_build_array(jsonb_build_object(
                    'kind','OPTION','symbol',leg->>'symbol',
                    'signed_quantity',intent.quantity::text,'multiplier',100));
            ELSE RAISE EXCEPTION 'competition position intent side is invalid';
        END CASE;
    END LOOP;
    IF source_action IN ('ENTRY','ROLL') THEN
        IF opening_count <> 2 OR buy_open <> 1 OR sell_open <> 1
           OR competition_record_spread(opening_inventory) IS NULL
           OR competition_record_spread(source_inventory) IS DISTINCT FROM
                competition_record_spread(opening_inventory)
        THEN RAISE EXCEPTION 'competition opening vertical is invalid'; END IF;
    ELSIF source_inventory <> '[]'::jsonb THEN
        RAISE EXCEPTION 'competition close retained inventory';
    END IF;
    IF source_action IN ('CLOSE','ROLL') THEN
        IF closing_count <> 2 OR buy_close <> 1 OR sell_close <> 1
           OR competition_record_spread(closing_inventory) IS NULL
           OR competition_record_spread(closing_inventory) IS DISTINCT FROM
                competition_record_spread(predecessor_inventory)
        THEN RAISE EXCEPTION 'competition closing vertical is invalid'; END IF;
    ELSIF predecessor_inventory <> '[]'::jsonb THEN
        RAISE EXCEPTION 'competition entry has a predecessor inventory';
    END IF;
    IF source_action = 'ENTRY' AND closing_count <> 0
       OR source_action = 'CLOSE' AND opening_count <> 0
    THEN RAISE EXCEPTION 'competition position intent action is invalid'; END IF;
    RETURN true;
END
$function$;

CREATE FUNCTION competition_record_source_hash(
    kind text,
    source_decision_id uuid,
    source_position_id uuid
) RETURNS varchar(64) STABLE LANGUAGE plpgsql AS $function$
DECLARE
    account account_roles%%ROWTYPE;
    baseline submission_baselines%%ROWTYPE;
    decision_hashes jsonb := '[]'::jsonb;
    position_hashes jsonb := '[]'::jsonb;
    closed_at_text text;
BEGIN
    SELECT * INTO account FROM account_roles WHERE role = 'SUBMISSION';
    SELECT * INTO baseline FROM submission_baselines WHERE account_role = 'SUBMISSION';
    IF account.role IS NULL OR baseline.baseline_id IS NULL
       OR baseline.account_fingerprint IS DISTINCT FROM account.account_fingerprint
       OR baseline.equity IS DISTINCT FROM 100000::numeric
       OR baseline.contaminated
       OR baseline.positions_hash !~ '^[0-9a-f]{64}$'
       OR baseline.orders_hash !~ '^[0-9a-f]{64}$'
       OR baseline.activities_hash !~ '^[0-9a-f]{64}$'
    THEN RAISE EXCEPTION 'competition submission baseline is not clean and current'; END IF;
    IF kind = 'NO_TRADE' THEN
        SELECT jsonb_build_array(result_hash) INTO decision_hashes
          FROM agent_decisions
         WHERE agent_decisions.decision_id = source_decision_id;
        IF decision_hashes IS NULL THEN
            RAISE EXCEPTION 'competition no-trade source is missing';
        END IF;
    ELSIF kind = 'POSITION' THEN
        SELECT jsonb_build_array(thesis.thesis_hash)
            || COALESCE((
                SELECT jsonb_agg(transition.transition_hash ORDER BY transition.transition_sequence)
                FROM managed_position_transitions transition
                WHERE transition.managed_position_id = position.managed_position_id
            ), '[]'::jsonb)
            || COALESCE((
                SELECT jsonb_agg(snapshot.snapshot_hash ORDER BY transition.transition_sequence)
                FROM managed_position_snapshots snapshot
                JOIN managed_position_transitions transition
                  ON transition.transition_id = snapshot.transition_id
                WHERE snapshot.managed_position_id = position.managed_position_id
            ), '[]'::jsonb)
            || COALESCE((
                SELECT jsonb_agg(decision.result_hash
                    ORDER BY decision.decision_boundary, decision.decision_id)
                FROM agent_decisions decision
                JOIN agent_input_snapshots input
                  ON input.snapshot_id = decision.input_snapshot_id
                JOIN lifecycle_observation_bindings binding
                  ON binding.agent_input_snapshot_id = input.snapshot_id
                JOIN lifecycle_observation_manifests manifest
                  ON manifest.manifest_id = binding.manifest_id
                WHERE manifest.managed_position_id = position.managed_position_id
                  AND decision.account_role = 'SUBMISSION'
                  AND decision.account_fingerprint = position.account_fingerprint
                  AND decision.decision_kind = 'ASSESSMENT'
                  AND decision.thesis_version_id = position.thesis_version_id
                  AND input.account_role = 'SUBMISSION'
                  AND input.account_fingerprint = position.account_fingerprint
                  AND input.decision_kind = 'ASSESSMENT'
                  AND input.thesis_version_id = position.thesis_version_id
            ), '[]'::jsonb)
            || jsonb_build_array(position.active_position_fingerprint),
            competition_record_utc_text(position.closed_at)
          INTO position_hashes, closed_at_text
          FROM managed_lifecycle_positions position
          JOIN thesis_versions thesis
            ON thesis.thesis_version_id = position.thesis_version_id
         WHERE position.managed_position_id = source_position_id
           AND position.account_role = 'SUBMISSION'
           AND position.account_fingerprint = account.account_fingerprint
           AND thesis.account_role = 'SUBMISSION';
        IF position_hashes IS NULL THEN
            RAISE EXCEPTION 'competition position source is missing';
        END IF;
    ELSE
        RAISE EXCEPTION 'competition record kind is invalid';
    END IF;
    RETURN opportunity_plain_hash(jsonb_build_object(
        'domain', 'alphadecay.competition-record-source.v1',
        'record_kind', kind,
        'baseline', jsonb_build_object(
            'baseline_id', baseline.baseline_id::text,
            'account_fingerprint', baseline.account_fingerprint,
            'captured_at', competition_record_utc_text(baseline.captured_at),
            'positions_hash', baseline.positions_hash,
            'orders_hash', baseline.orders_hash,
            'activities_hash', baseline.activities_hash
        ),
        'decision_hashes', decision_hashes,
        'position_hashes', position_hashes,
        'closed_at', closed_at_text
    ));
END
$function$;

CREATE FUNCTION competition_record_events(source_position_id uuid)
RETURNS jsonb STABLE LANGUAGE plpgsql AS $function$
DECLARE
    events jsonb;
BEGIN
    SELECT COALESCE(jsonb_agg(event ORDER BY occurred_at, event_priority, action), '[]'::jsonb)
      INTO events
      FROM (
        SELECT jsonb_build_object(
                'event_kind', 'EXECUTION',
                'action', transition.action,
                'occurred_at', competition_record_utc_text(transition.occurred_at),
                'reason_category', CASE transition.action
                    WHEN 'ENTRY' THEN 'POSITION_OPENED'
                    WHEN 'ROLL' THEN 'POSITION_ROLLED'
                    ELSE 'POSITION_CLOSED' END,
                'cashflow_usd', transition.cashflow_contribution::text,
                'execution_status', certificate.execution_status,
                'resulting_state', CASE transition.action WHEN 'CLOSE' THEN 'CLOSED' ELSE 'OPEN' END,
                'spread_after', CASE transition.action WHEN 'CLOSE' THEN NULL
                    ELSE competition_record_spread(snapshot.normalized_inventory) END
            ) AS event,
            transition.occurred_at AS occurred_at,
            0 AS event_priority,
            transition.action AS action
          FROM managed_position_transitions transition
          JOIN managed_position_snapshots snapshot
            ON snapshot.transition_id = transition.transition_id
          LEFT JOIN managed_position_transitions predecessor
            ON predecessor.transition_id = transition.predecessor_transition_id
          LEFT JOIN managed_position_snapshots predecessor_snapshot
            ON predecessor_snapshot.transition_id = predecessor.transition_id
          JOIN execution_intents intent
            ON intent.intent_id = transition.execution_intent_id
          JOIN execution_certificates certificate
            ON certificate.certificate_id = transition.execution_certificate_id
         WHERE transition.managed_position_id = source_position_id
           AND intent.account_role = 'SUBMISSION'
           AND intent.state = 'TERMINAL'
           AND intent.action = transition.action
           AND certificate.execution_intent_id = intent.intent_id
           AND certificate.execution_status = 'FILLED'
           AND competition_record_intent_matches_snapshot(
                intent.intent_id,
                transition.action,
                snapshot.normalized_inventory,
                COALESCE(predecessor_snapshot.normalized_inventory, '[]'::jsonb)
           )
        UNION ALL
        SELECT jsonb_build_object(
                'event_kind', 'ASSESSMENT',
                'action', CASE decision.outcome
                    WHEN 'HOLD_CERTIFIED' THEN 'HOLD'
                    WHEN 'CLOSE_RISK_ONLY' THEN 'CLOSE'
                    WHEN 'CLOSE_APPROVED' THEN 'CLOSE'
                    WHEN 'ROLL_APPROVED' THEN 'ROLL'
                    WHEN 'NO_ACTION' THEN 'NO_ACTION' END,
                'occurred_at', competition_record_utc_text(decision.decision_boundary),
                'reason_category', CASE decision.outcome
                    WHEN 'HOLD_CERTIFIED' THEN 'POSITION_REVIEWED'
                    WHEN 'CLOSE_RISK_ONLY' THEN 'RISK_REDUCTION'
                    WHEN 'CLOSE_APPROVED' THEN 'THESIS_CHANGED'
                    WHEN 'ROLL_APPROVED' THEN 'POSITION_ADJUSTMENT'
                    WHEN 'NO_ACTION' THEN 'DATA_INCOMPLETE' END
            ) AS event,
            decision.decision_boundary AS occurred_at,
            1 AS event_priority,
            decision.outcome AS action
          FROM agent_decisions decision
          JOIN agent_input_snapshots input
            ON input.snapshot_id = decision.input_snapshot_id
          JOIN lifecycle_observation_bindings binding
            ON binding.agent_input_snapshot_id = input.snapshot_id
          JOIN lifecycle_observation_manifests manifest
            ON manifest.manifest_id = binding.manifest_id
          JOIN managed_lifecycle_positions position
            ON position.managed_position_id = manifest.managed_position_id
         WHERE position.managed_position_id = source_position_id
           AND decision.account_role = 'SUBMISSION'
           AND decision.account_fingerprint = position.account_fingerprint
           AND decision.decision_kind = 'ASSESSMENT'
           AND decision.thesis_version_id = position.thesis_version_id
           AND input.account_role = 'SUBMISSION'
           AND input.account_fingerprint = position.account_fingerprint
           AND input.decision_kind = 'ASSESSMENT'
           AND input.thesis_version_id = position.thesis_version_id
           AND decision.outcome IN (
                'HOLD_CERTIFIED','CLOSE_RISK_ONLY','CLOSE_APPROVED','ROLL_APPROVED','NO_ACTION'
           )
      ) ordered_events;
    RETURN events;
END
$function$;

CREATE FUNCTION competition_record_expected_projection(
    kind text,
    source_decision_id uuid,
    source_position_id uuid
) RETURNS jsonb STABLE LANGUAGE plpgsql AS $function$
DECLARE
    decision agent_decisions%%ROWTYPE;
    input agent_input_snapshots%%ROWTYPE;
    position managed_lifecycle_positions%%ROWTYPE;
    thesis thesis_versions%%ROWTYPE;
    opening_transition managed_position_transitions%%ROWTYPE;
    latest_transition managed_position_transitions%%ROWTYPE;
    opening_snapshot managed_position_snapshots%%ROWTYPE;
    latest_snapshot managed_position_snapshots%%ROWTYPE;
    latest_certificate execution_certificates%%ROWTYPE;
    events jsonb;
    as_of timestamptz;
BEGIN
    IF kind = 'NO_TRADE' THEN
        SELECT * INTO decision FROM agent_decisions
         WHERE agent_decisions.decision_id = source_decision_id;
        SELECT * INTO input FROM agent_input_snapshots WHERE snapshot_id = decision.input_snapshot_id;
        IF decision.account_role <> 'SUBMISSION'
           OR input.account_role <> 'SUBMISSION'
           OR decision.account_fingerprint IS DISTINCT FROM input.account_fingerprint
           OR decision.decision_kind <> 'OPPORTUNITY'
           OR input.decision_kind <> 'OPPORTUNITY'
           OR decision.decision_boundary IS DISTINCT FROM input.decision_boundary
           OR decision.outcome <> 'NO_TRADE'
           OR decision.reason_code <> 'CALIBRATION_BINDING_NO_TRADE'
           OR decision.autonomy_authorized
           OR decision.thesis_version_id IS NOT NULL
           OR input.thesis_version_id IS NOT NULL
        THEN RAISE EXCEPTION 'competition no-trade source authority mismatch'; END IF;
        RETURN jsonb_build_object(
            'schema_version', 'v1',
            'record_kind', 'NO_TRADE',
            'public_record_id', competition_record_public_id('NO_TRADE', decision.result_hash),
            'status', 'NO_TRADE',
            'reason_category', 'STRATEGY_NOT_READY',
            'decided_at', competition_record_utc_text(decision.decision_boundary),
            'observed_at', competition_record_utc_text(input.observed_at),
            'paper_trading', true
        );
    END IF;

    SELECT * INTO position FROM managed_lifecycle_positions
      WHERE managed_position_id = source_position_id AND account_role = 'SUBMISSION';
    SELECT * INTO thesis FROM thesis_versions
      WHERE thesis_version_id = position.thesis_version_id AND account_role = 'SUBMISSION';
    SELECT * INTO opening_transition FROM managed_position_transitions
      WHERE managed_position_id = source_position_id
        AND transition_sequence = 0 AND action = 'ENTRY';
    SELECT * INTO latest_transition FROM managed_position_transitions
      WHERE managed_position_id = source_position_id
      ORDER BY transition_sequence DESC LIMIT 1;
    SELECT * INTO opening_snapshot FROM managed_position_snapshots
      WHERE transition_id = opening_transition.transition_id;
    SELECT * INTO latest_snapshot FROM managed_position_snapshots
      WHERE transition_id = latest_transition.transition_id;
    SELECT * INTO latest_certificate FROM execution_certificates
      WHERE certificate_id = latest_transition.execution_certificate_id;
    IF position.managed_position_id IS NULL OR thesis.thesis_version_id IS NULL
       OR opening_transition.transition_id IS NULL OR latest_transition.transition_id IS NULL
       OR opening_snapshot.snapshot_id IS NULL OR latest_snapshot.snapshot_id IS NULL
       OR latest_certificate.certificate_id IS NULL
       OR latest_certificate.execution_status <> 'FILLED'
       OR position.current_snapshot_id IS DISTINCT FROM latest_snapshot.snapshot_id
       OR position.active_position_fingerprint IS DISTINCT FROM latest_snapshot.position_fingerprint
       OR position.activated_at IS DISTINCT FROM opening_transition.occurred_at
       OR (position.closed_at IS NULL) IS DISTINCT FROM (latest_transition.action <> 'CLOSE')
       OR (position.closed_at IS NOT NULL AND position.closed_at IS DISTINCT FROM latest_transition.occurred_at)
       OR competition_record_spread(opening_snapshot.normalized_inventory)->>'underlying'
            IS DISTINCT FROM thesis.underlying
       OR (
            latest_transition.action <> 'CLOSE'
            AND competition_record_spread(latest_snapshot.normalized_inventory)->>'underlying'
                IS DISTINCT FROM thesis.underlying
       )
    THEN RAISE EXCEPTION 'competition position source authority mismatch'; END IF;
    events := competition_record_events(source_position_id);
    IF jsonb_array_length(events) = 0 THEN
        RAISE EXCEPTION 'competition position event history is empty';
    END IF;
    SELECT greatest(
        latest_snapshot.accepted_at,
        latest_certificate.created_at,
        max((event->>'occurred_at')::timestamptz)
    ) INTO as_of FROM jsonb_array_elements(events) event;
    RETURN jsonb_build_object(
        'schema_version', 'v1',
        'record_kind', 'POSITION',
        'public_record_id', competition_record_public_id(
            'POSITION', opening_transition.transition_hash
        ),
        'state', CASE latest_transition.action WHEN 'CLOSE' THEN 'CLOSED' ELSE 'OPEN' END,
        'underlying', thesis.underlying,
        'opening_spread', competition_record_spread(opening_snapshot.normalized_inventory),
        'current_spread', CASE latest_transition.action WHEN 'CLOSE' THEN NULL
            ELSE competition_record_spread(latest_snapshot.normalized_inventory) END,
        'opened_at', competition_record_utc_text(position.activated_at),
        'as_of', competition_record_utc_text(as_of),
        'closed_at', competition_record_utc_text(position.closed_at),
        'thesis', jsonb_build_object(
            'direction', CASE
                WHEN (thesis.intended_exposure->>'delta')::numeric > 0 THEN 'BULLISH'
                WHEN (thesis.intended_exposure->>'delta')::numeric < 0 THEN 'BEARISH'
                ELSE 'NEUTRAL' END,
            'volatility_view', thesis.volatility_view,
            'target_at', competition_record_utc_text(thesis.target_at)
        ),
        'events', events,
        'current_exposure', competition_record_exposure(latest_certificate.actual_exposure),
        'execution_status', latest_certificate.execution_status,
        'paper_trading', true
    );
END
$function$;

CREATE OR REPLACE FUNCTION enforce_competition_record_publication_insert()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    prior competition_record_publications%%ROWTYPE;
    payload jsonb;
    projection jsonb;
    expected_projection jsonb;
    expected_source_hash varchar(64);
    expected_publication_hash varchar(64);
BEGIN
    PERFORM pg_advisory_xact_lock(6748832221);
    BEGIN
        payload := NEW.payload_text::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'competition record payload is not JSON';
    END;
    IF jsonb_typeof(payload) <> 'object'
       OR NEW.payload_text IS DISTINCT FROM opportunity_canonical_json(payload)
       OR NOT (payload ?& ARRAY['published_at','publication_hash','predecessor_hash'])
    THEN RAISE EXCEPTION 'competition record payload is not canonical'; END IF;
    projection := payload - ARRAY['published_at','publication_hash','predecessor_hash'];
    expected_projection := competition_record_expected_projection(
        NEW.record_kind, NEW.source_decision_id, NEW.source_managed_position_id
    );
    expected_source_hash := competition_record_source_hash(
        NEW.record_kind, NEW.source_decision_id, NEW.source_managed_position_id
    );
    expected_publication_hash := opportunity_plain_hash(jsonb_build_object(
        'domain', 'alphadecay.competition-record-publication.v1',
        'public_record_id', NEW.public_record_id,
        'source_authority_hash', expected_source_hash,
        'projection_hash', opportunity_plain_hash(expected_projection),
        'published_at', competition_record_utc_text(NEW.published_at),
        'predecessor_hash', NEW.predecessor_hash
    ));
    IF projection IS DISTINCT FROM expected_projection
       OR NEW.source_authority_hash IS DISTINCT FROM expected_source_hash
       OR NEW.public_record_id IS DISTINCT FROM expected_projection->>'public_record_id'
       OR NEW.occurred_at IS DISTINCT FROM (CASE NEW.record_kind
            WHEN 'NO_TRADE' THEN (expected_projection->>'decided_at')::timestamptz
            ELSE (expected_projection->>'opened_at')::timestamptz END)
       OR NEW.projection_hash IS DISTINCT FROM opportunity_plain_hash(expected_projection)
       OR payload->>'published_at' IS DISTINCT FROM competition_record_utc_text(NEW.published_at)
       OR payload->>'publication_hash' IS DISTINCT FROM expected_publication_hash
       OR payload->>'predecessor_hash' IS DISTINCT FROM NEW.predecessor_hash
       OR NEW.publication_hash IS DISTINCT FROM expected_publication_hash
    THEN RAISE EXCEPTION 'competition record payload does not match source authority'; END IF;

    SELECT * INTO prior FROM competition_record_publications
      ORDER BY published_at DESC, publication_id DESC LIMIT 1;
    IF FOUND THEN
        IF NEW.published_at <= prior.published_at
           OR NEW.predecessor_hash IS DISTINCT FROM prior.publication_hash
        THEN RAISE EXCEPTION 'competition record publication chain is broken'; END IF;
    ELSIF NEW.predecessor_hash IS NOT NULL THEN
        RAISE EXCEPTION 'genesis competition record has a predecessor';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER competition_record_publication_insert_guard
BEFORE INSERT ON competition_record_publications
FOR EACH ROW EXECUTE FUNCTION enforce_competition_record_publication_insert();

CREATE OR REPLACE FUNCTION reject_competition_record_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    RAISE EXCEPTION 'competition record publications are append-only';
END
$function$;

CREATE TRIGGER competition_record_publications_append_only
BEFORE UPDATE OR DELETE ON competition_record_publications
FOR EACH ROW EXECUTE FUNCTION reject_competition_record_mutation();
