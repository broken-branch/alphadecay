from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from pg8000.dbapi import ProgrammingError as PGProgrammingError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import PositionIntent
from backend.app.execution import (
    ExecutionAction,
    OrderEnvelope,
    OrderLegIntent,
    intent_digest,
    order_envelope_hash,
)
from backend.app.lifecycle.fingerprint import option_position_fingerprint
from backend.app.lifecycle.repository import LifecycleResearchSource, SQLAlchemyLifecycleRepository
from backend.app.persistence.runtime import apply_migrations, discover_migrations
from backend.tests.runtime_composition.test_development_acquisition import (
    NOW,
    classification,
    cluster,
    observation,
)
from backend.tests.runtime_composition.test_development_acquisition import (
    context as lifecycle_context,
)

POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"
MIGRATIONS = Path(__file__).parents[3] / "migrations"

ENTRY_LONG = "NVDA260918C00170000"
ENTRY_SHORT = "NVDA260918C00180000"
ROLL_LONG = "NVDA260925C00170000"
ROLL_SHORT = "NVDA260925C00180000"
ENTRY_INVENTORY = [
    {"kind": "OPTION", "symbol": ENTRY_LONG, "signed_quantity": "1", "multiplier": 100},
    {"kind": "OPTION", "symbol": ENTRY_SHORT, "signed_quantity": "-1", "multiplier": 100},
]
ROLL_INVENTORY = [
    {"kind": "OPTION", "symbol": ROLL_LONG, "signed_quantity": "1", "multiplier": 100},
    {"kind": "OPTION", "symbol": ROLL_SHORT, "signed_quantity": "-1", "multiplier": 100},
]
ENTRY_ACTIVITIES = [
    {
        "activity_id_hash": "a" * 64,
        "activity_type": "OPTRD",
        "occurred_at": "2026-08-29T15:00:00+00:00",
        "symbol": ENTRY_LONG,
        "signed_quantity": "1",
        "provider_order_id": "provider-entry",
        "client_order_id": "client-entry",
    },
    {
        "activity_id_hash": "b" * 64,
        "activity_type": "OPTRD",
        "occurred_at": "2026-08-29T15:00:00+00:00",
        "symbol": ENTRY_SHORT,
        "signed_quantity": "-1",
        "provider_order_id": "provider-entry",
        "client_order_id": "client-entry",
    },
]
ROLL_FILL_ACTIVITIES = [
    {
        "activity_id_hash": activity_hash * 64,
        "activity_type": "OPTRD",
        "occurred_at": "2026-08-30T15:00:00+00:00",
        "symbol": symbol,
        "signed_quantity": quantity,
        "provider_order_id": "provider-roll",
        "client_order_id": "client-roll",
    }
    for activity_hash, symbol, quantity in (
        ("c", ENTRY_LONG, "-1"),
        ("d", ENTRY_SHORT, "1"),
        ("e", ROLL_LONG, "1"),
        ("f", ROLL_SHORT, "-1"),
    )
]
ENTRY_LEGS = [
    {"symbol": ENTRY_LONG, "intent": "BUY_TO_OPEN", "ratio": 1},
    {"symbol": ENTRY_SHORT, "intent": "SELL_TO_OPEN", "ratio": 1},
]
ROLL_LEGS = [
    {"symbol": ENTRY_LONG, "intent": "SELL_TO_CLOSE", "ratio": 1},
    {"symbol": ENTRY_SHORT, "intent": "BUY_TO_CLOSE", "ratio": 1},
    {"symbol": ROLL_LONG, "intent": "BUY_TO_OPEN", "ratio": 1},
    {"symbol": ROLL_SHORT, "intent": "SELL_TO_OPEN", "ratio": 1},
]
CLOSE_LEGS = ROLL_LEGS[:2]


def _envelope(action: ExecutionAction, certificate_id: UUID, legs: list[dict[str, object]]):
    return OrderEnvelope(
        action=action,
        authorization_certificate_id=certificate_id,
        policy_hash="b" * 64,
        account_fingerprint="a" * 64,
        position_or_book_fingerprint=(
            "2" * 64
            if action == ExecutionAction.ENTRY
            else option_position_fingerprint(
                ((ENTRY_LONG, Decimal("1"), 100), (ENTRY_SHORT, Decimal("-1"), 100))
            )
        ),
        legs=tuple(
            OrderLegIntent(
                symbol=str(leg["symbol"]),
                intent=PositionIntent(str(leg["intent"])),
                ratio=int(leg["ratio"]),
            )
            for leg in legs
        ),
        quantity=1,
        minimum_limit=Decimal("1"),
        maximum_limit=Decimal("2"),
        approved_max_loss=Decimal("500"),
        event_key="ENTRY-1" if action == ExecutionAction.ENTRY else "ROLL-1",
        trading_day=date(2026, 8, 29) if action == ExecutionAction.ENTRY else date(2026, 8, 30),
        market_session_id=(
            UUID("10000000-0000-0000-0000-000000000002") if action == ExecutionAction.ROLL else None
        ),
        quoted_relative_spread=(Decimal("0.05") if action == ExecutionAction.ROLL else None),
        maximum_relative_spread=(Decimal("0.25") if action == ExecutionAction.ROLL else None),
        incremental_debit=(Decimal("100") if action == ExecutionAction.ROLL else None),
        maximum_incremental_debit=(Decimal("500") if action == ExecutionAction.ROLL else None),
    )


def _envelope_payload(envelope: OrderEnvelope) -> dict[str, object]:
    return {
        "action": envelope.action.value,
        "authorization_certificate_id": str(envelope.authorization_certificate_id),
        "policy_hash": envelope.policy_hash,
        "account_fingerprint": envelope.account_fingerprint,
        "position_or_book_fingerprint": envelope.position_or_book_fingerprint,
        "legs": [
            {"symbol": leg.symbol, "intent": leg.intent.value, "ratio": leg.ratio}
            for leg in envelope.legs
        ],
        "quantity": envelope.quantity,
        "minimum_limit": str(envelope.minimum_limit),
        "maximum_limit": str(envelope.maximum_limit),
        "approved_max_loss": str(envelope.approved_max_loss),
        "event_key": envelope.event_key,
        "trading_day": envelope.trading_day.isoformat(),
        "market_session_id": (
            str(envelope.market_session_id) if envelope.market_session_id is not None else None
        ),
        "quoted_relative_spread": (
            str(envelope.quoted_relative_spread)
            if envelope.quoted_relative_spread is not None
            else None
        ),
        "maximum_relative_spread": (
            str(envelope.maximum_relative_spread)
            if envelope.maximum_relative_spread is not None
            else None
        ),
        "incremental_debit": (
            str(envelope.incremental_debit) if envelope.incremental_debit is not None else None
        ),
        "maximum_incremental_debit": (
            str(envelope.maximum_incremental_debit)
            if envelope.maximum_incremental_debit is not None
            else None
        ),
    }


ENTRY_ENVELOPE = _envelope(
    ExecutionAction.ENTRY,
    UUID("20000000-0000-0000-0000-000000000001"),
    ENTRY_LEGS,
)
ROLL_ENVELOPE = _envelope(
    ExecutionAction.ROLL,
    UUID("30000000-0000-0000-0000-000000000001"),
    ROLL_LEGS,
)
CLOSE_ENVELOPE = _envelope(
    ExecutionAction.CLOSE,
    UUID("30000000-0000-0000-0000-000000000001"),
    CLOSE_LEGS,
)

pytestmark = pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)


def test_0014_upgrades_0013_and_replaces_the_market_session_guard() -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"lifecycle_provider_upgrade_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ[POSTGRES_URL_ENV],
        connect_args={"startup_params": {"search_path": schema}},
    )
    migrations = discover_migrations(MIGRATIONS)
    try:
        apply_migrations(engine, migrations[:13])
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid=t.tgrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=current_schema() AND tgname="
                        "'alpaca_market_session_unavailable_guard' AND NOT tgisinternal"
                    )
                ).scalar_one()
                == 1
            )
        apply_migrations(engine, migrations)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT array_agg(version ORDER BY version) FROM alphadecay_schema_migrations")
            ).scalar_one() == list(range(1, len(migrations) + 1))
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid=t.tgrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=current_schema() AND tgname="
                        "'alpaca_market_session_unavailable_guard' AND NOT tgisinternal"
                    )
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid=t.tgrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=current_schema() AND tgname="
                        "'alpaca_market_session_provider_guard' AND NOT tgisinternal"
                    )
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_0016_upgrades_0015_restarts_and_matches_application_fingerprint() -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"managed_lineage_upgrade_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ[POSTGRES_URL_ENV],
        connect_args={"startup_params": {"search_path": schema}},
    )
    migrations = discover_migrations(MIGRATIONS)
    inventory = (
        ("NVDA260918C00170000", Decimal("1.0"), 100),
        ("NVDA260918C00180000", Decimal("-1.00"), 100),
    )
    payload = [
        {
            "signed_quantity": str(int(quantity)),
            "symbol": symbol,
            "multiplier": multiplier,
            "kind": "OPTION",
        }
        for symbol, quantity, multiplier in inventory
    ]
    try:
        apply_migrations(engine, migrations[:15])
        apply_migrations(engine, migrations)
        engine.dispose()
        apply_migrations(engine, migrations)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT array_agg(version ORDER BY version) FROM alphadecay_schema_migrations")
            ).scalar_one() == list(range(1, len(migrations) + 1))
            assert connection.execute(
                text("SELECT lifecycle_position_fingerprint(CAST(:inventory AS jsonb))"),
                {"inventory": json.dumps(payload)},
            ).scalar_one() == option_position_fingerprint(inventory)
            assert connection.execute(
                text("SELECT lifecycle_position_fingerprint('[]'::jsonb)")
            ).scalar_one() == option_position_fingerprint(())
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_0016_upgrade_rejects_legacy_managed_position_history() -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"managed_lineage_history_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ[POSTGRES_URL_ENV],
        connect_args={"startup_params": {"search_path": schema}},
    )
    migrations = discover_migrations(MIGRATIONS)
    try:
        apply_migrations(engine, migrations[:15])
        with engine.begin() as connection:
            connection.exec_driver_sql("SET session_replication_role = replica")
            connection.exec_driver_sql(
                """
                INSERT INTO managed_lifecycle_positions(
                  managed_position_id,account_role,account_fingerprint,
                  entry_execution_certificate_id,entry_intent_id,entry_approval_id,
                  thesis_version_id,entry_reconciliation_id,current_reconciliation_state_id,
                  current_snapshot_id,active_position_fingerprint,activated_at)
                VALUES ('10000000-0000-0000-0000-000000000001','DEVELOPMENT',repeat('a',64),
                  '20000000-0000-0000-0000-000000000001',
                  '30000000-0000-0000-0000-000000000001',
                  '40000000-0000-0000-0000-000000000001',
                  '50000000-0000-0000-0000-000000000001',
                  '60000000-0000-0000-0000-000000000001',
                  '70000000-0000-0000-0000-000000000001',NULL,repeat('b',64),now())
                """
            )
            connection.exec_driver_sql("SET session_replication_role = origin")
        with pytest.raises(
            PGProgrammingError,
            match="MANAGED_POSITION_LINEAGE_CONTRACT_REQUIRES_ZERO_HISTORY",
        ):
            apply_migrations(engine, migrations)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize(
    "inventory",
    (
        '[{"kind":"EQUITY","symbol":"NVDA","signed_quantity":"1","multiplier":1}]',
        '[{"kind":"OPTION","symbol":"NVDA260918C00170000","signed_quantity":"1",'
        '"multiplier":100,"extra":true}]',
        '[{"kind":"OPTION","symbol":"NVDA260918C00170000","signed_quantity":"1.0",'
        '"multiplier":100}]',
    ),
    ids=("equity", "extra-field", "noncanonical-quantity"),
)
def test_position_fingerprint_rejects_noncanonical_inventory(
    lifecycle_engine, inventory: str
) -> None:
    with (
        pytest.raises(
            DBAPIError,
            match="MANAGED_POSITION_FINGERPRINT_INVENTORY_INVALID",
        ),
        lifecycle_engine.connect() as connection,
    ):
        connection.execute(
            text("SELECT lifecycle_position_fingerprint(CAST(:inventory AS jsonb))"),
            {"inventory": inventory},
        )


def test_0012_upgrade_rejects_historical_assessment_authority() -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"lifecycle_upgrade_v5_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ[POSTGRES_URL_ENV],
        connect_args={"startup_params": {"search_path": schema}},
    )
    migrations = discover_migrations(MIGRATIONS)
    try:
        apply_migrations(engine, migrations[:12])
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO account_roles(role,account_fingerprint,equity,autonomous_enabled) "
                "VALUES ('DEVELOPMENT',repeat('a',64),100000,false)"
            )
            connection.exec_driver_sql(
                """
                INSERT INTO assessment_certificates(certificate_id,assessment_id,account_role,
                  action,position_fingerprint,envelope_hash,approved_max_loss,quantity,policy_hash,
                  created_at,expires_at,valid) VALUES
                  ('10000000-0000-0000-0000-000000000001',
                   '10000000-0000-0000-0000-000000000002','DEVELOPMENT','CLOSE',repeat('b',64),
                   repeat('c',64),10,1,repeat('d',64),'2026-08-29 00:00+00',
                   '2026-08-29 01:00+00',true)
                """
            )
        with pytest.raises(
            PGProgrammingError,
            match="LIFECYCLE_AUTHORITY_REQUIRES_VERIFIED_ZERO_HISTORY",
        ):
            apply_migrations(engine, migrations)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize(
    "authority_table",
    (
        "submission_baselines",
        "competition_entry_budget",
        "model_call_budgets",
        "evidence_classification_claims",
        "evidence_classifications",
    ),
)
def test_0012_upgrade_rejects_surviving_claim_gate_authority(authority_table: str) -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"lifecycle_claim_gate_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ[POSTGRES_URL_ENV],
        connect_args={"startup_params": {"search_path": schema}},
    )
    migrations = discover_migrations(MIGRATIONS)
    try:
        apply_migrations(engine, migrations[:12])
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO account_roles(role,account_fingerprint,equity,autonomous_enabled) "
                "VALUES ('SUBMISSION',repeat('a',64),100000,false)"
            )
            if authority_table == "submission_baselines":
                connection.exec_driver_sql(
                    "INSERT INTO submission_baselines(baseline_id,account_role,"
                    "account_fingerprint,equity,captured_at,positions_hash,orders_hash,"
                    "activities_hash) VALUES ('10000000-0000-0000-0000-000000000001',"
                    "'SUBMISSION',repeat('a',64),100000,now(),repeat('1',64),"
                    "repeat('2',64),repeat('3',64))"
                )
            elif authority_table == "competition_entry_budget":
                connection.exec_driver_sql(
                    "INSERT INTO competition_entry_budget(account_role) VALUES ('SUBMISSION')"
                )
            elif authority_table == "model_call_budgets":
                connection.exec_driver_sql(
                    "UPDATE model_call_budgets SET request_count=request_count+1 "
                    "WHERE model='gemini-3.7-flash'"
                )
            elif authority_table == "evidence_classification_claims":
                connection.exec_driver_sql(
                    "INSERT INTO evidence_classification_claims(evidence_hash,state,generation,"
                    "lease_owner,lease_expires_at,updated_at) VALUES (repeat('4',64),'PENDING',1,"
                    "'10000000-0000-0000-0000-000000000004',now()+interval '1 hour',now())"
                )
            else:
                connection.exec_driver_sql(
                    "INSERT INTO evidence_classification_claims(evidence_hash,state,generation,"
                    "updated_at) VALUES (repeat('5',64),'COMPLETED',1,now()); "
                    "INSERT INTO evidence_classifications(evidence_hash,classifications_payload,"
                    "classification_hash,completed_generation,completed_at) VALUES "
                    "(repeat('5',64),'[{}]'::jsonb,repeat('ab',32),1,now())"
                )
        with pytest.raises(
            PGProgrammingError,
            match="LIFECYCLE_AUTHORITY_REQUIRES_VERIFIED_ZERO_HISTORY",
        ):
            apply_migrations(engine, migrations)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_same_transaction_cannot_cross_bind_agent_and_thesis_authority(
    lifecycle_engine,
) -> None:
    with (
        pytest.raises(DBAPIError, match="AGENT_DECISION_THESIS_AUTHORITY_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            """
            INSERT INTO account_roles(role,account_fingerprint,equity,autonomous_enabled)
              VALUES ('DEVELOPMENT',repeat('a',64),100000,true);
            INSERT INTO competition_entry_budget(account_role) VALUES ('DEVELOPMENT');
            INSERT INTO thesis_versions(thesis_version_id,thesis_id,account_role,version,
              thesis_hash,policy_hash,underlying,thesis_code,frozen_at,target_at,
              intended_exposure,exposure_limits,volatility_view,entry_atm_iv,
              approved_max_loss,portfolio_risk_cap,invalidation_codes,thesis_payload,created_at)
              VALUES
              ('10000000-0000-0000-0000-000000000011',
               '10000000-0000-0000-0000-000000000012','DEVELOPMENT',1,repeat('1',64),
               repeat('b',64),'NVDA','FIRST','2026-08-29 00:00+00',
               '2026-09-04 00:00+00','{}','{}','LONG',0.4,500,500,'["FIRST"]','{}',
               '2026-08-29 00:00+00'),
              ('10000000-0000-0000-0000-000000000021',
               '10000000-0000-0000-0000-000000000022','DEVELOPMENT',2,repeat('2',64),
               repeat('b',64),'NVDA','SECOND','2026-08-29 00:00+00',
               '2026-09-04 00:00+00','{}','{}','LONG',0.4,500,500,'["SECOND"]','{}',
               '2026-08-29 00:00+00');
            INSERT INTO agent_ticks(tick_id,account_role,account_fingerprint,tick_key,
              tick_boundary,actor,status,reservation_token,created_at) VALUES
              ('20000000-0000-0000-0000-000000000001','DEVELOPMENT',repeat('a',64),'tick',
               date_trunc('hour',current_timestamp),'SCHEDULER','RESERVED',
               '20000000-0000-0000-0000-000000000002',current_timestamp);
            INSERT INTO agent_input_snapshots(snapshot_id,thesis_version_id,account_role,
              account_fingerprint,decision_kind,decision_boundary,observed_at,
              normalized_payload,input_hash,created_at) VALUES
              ('30000000-0000-0000-0000-000000000001',
               '10000000-0000-0000-0000-000000000011','DEVELOPMENT',repeat('a',64),
               'OPPORTUNITY',date_trunc('hour',current_timestamp),current_timestamp,'{}',
               repeat('3',64),current_timestamp);
            INSERT INTO agent_decisions(decision_id,thesis_version_id,origin_tick_id,
              input_snapshot_id,account_role,account_fingerprint,decision_kind,outcome,
              reason_code,policy_hash,result_payload,result_hash,autonomy_authorized,
              decision_boundary,created_at) VALUES
              ('40000000-0000-0000-0000-000000000001',
               '10000000-0000-0000-0000-000000000021',
               '20000000-0000-0000-0000-000000000001',
               '30000000-0000-0000-0000-000000000001','DEVELOPMENT',repeat('a',64),
               'OPPORTUNITY','ENTRY_APPROVED','POLICY_APPROVED',repeat('b',64),'{}',
               repeat('4',64),true,date_trunc('hour',current_timestamp),current_timestamp);
            INSERT INTO entry_approval_certificates(approval_id,thesis_version_id,
              agent_decision_id,account_role,policy_hash,book_fingerprint,envelope_hash,
              approved_max_loss,quantity,valid_from,expires_at,valid) VALUES
              ('50000000-0000-0000-0000-000000000001',
               '10000000-0000-0000-0000-000000000021',
               '40000000-0000-0000-0000-000000000001','DEVELOPMENT',repeat('b',64),
               repeat('5',64),repeat('ab',32),500,1,current_timestamp-interval '1 hour',
               current_timestamp+interval '1 hour',true);
            INSERT INTO execution_intents(intent_id,account_role,intent_digest,action,policy_hash,
              event_key,trading_day,entry_approval_id,fingerprint,envelope_hash,envelope_payload,
              legs,quantity,minimum_limit,maximum_limit,approved_max_loss,state) VALUES
              ('60000000-0000-0000-0000-000000000001','DEVELOPMENT',repeat('7',64),'ENTRY',
               repeat('b',64),'EVENT',current_date,
               '50000000-0000-0000-0000-000000000001',repeat('5',64),repeat('ab',32),
               jsonb_build_object('account_fingerprint',repeat('a',64),
                 'authorization_certificate_id','50000000-0000-0000-0000-000000000001'),
               '[]',1,1,1,500,'APPROVED');
            UPDATE agent_ticks SET decision_id='40000000-0000-0000-0000-000000000001'
              WHERE tick_id='20000000-0000-0000-0000-000000000001';
            """
        )


@pytest.fixture
def lifecycle_engine():
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"lifecycle_v4_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ[POSTGRES_URL_ENV],
        connect_args={"startup_params": {"search_path": schema}},
    )
    apply_migrations(engine, discover_migrations(MIGRATIONS))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize(
    "null_field",
    (
        "quoted_relative_spread",
        "maximum_relative_spread",
        "incremental_debit",
        "maximum_incremental_debit",
    ),
)
def test_postgres_rejects_roll_intent_with_null_numeric_authority(
    lifecycle_engine,
    null_field: str,
) -> None:
    values = {
        "quoted_relative_spread": "0.05",
        "maximum_relative_spread": "0.25",
        "incremental_debit": "100",
        "maximum_incremental_debit": "500",
    }
    values[null_field] = "NULL"

    with (
        pytest.raises(DBAPIError, match="ck_intent_roll_authority"),
        lifecycle_engine.begin() as connection,
    ):
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            f"""
            INSERT INTO execution_intents(
              intent_id, account_role, intent_digest, action, policy_hash,
              event_key, trading_day, assessment_certificate_id, fingerprint,
              envelope_hash, envelope_payload, legs, quantity, minimum_limit,
              maximum_limit, approved_max_loss, market_session_id,
              quoted_relative_spread, maximum_relative_spread, incremental_debit,
              maximum_incremental_debit, state, first_fill_consumed
            ) VALUES (
              '10000000-0000-0000-0000-000000000001', 'DEVELOPMENT', repeat('1',64),
              'ROLL', repeat('2',64), 'ROLL-NULL-AUTHORITY', '2026-08-30',
              '30000000-0000-0000-0000-000000000001', repeat('3',64), repeat('4',64),
              '{{}}', '[]', 1, 1, 2, 500,
              '40000000-0000-0000-0000-000000000001',
              {values["quoted_relative_spread"]}, {values["maximum_relative_spread"]},
              {values["incremental_debit"]}, {values["maximum_incremental_debit"]},
              'APPROVED', false
            )
            """
        )


def _seed_transition_authority(engine, *, reconciliation_fingerprint: str, thesis_id: str) -> None:
    entry_inventory = json.dumps(ENTRY_INVENTORY, separators=(",", ":"))
    roll_inventory = json.dumps(ROLL_INVENTORY, separators=(",", ":"))
    entry_activities = json.dumps(ENTRY_ACTIVITIES, separators=(",", ":"))
    all_roll_activities = json.dumps(ENTRY_ACTIVITIES + ROLL_FILL_ACTIVITIES, separators=(",", ":"))
    entry_legs = json.dumps(ENTRY_LEGS, separators=(",", ":"))
    roll_legs = json.dumps(ROLL_LEGS, separators=(",", ":"))
    entry_payload = json.dumps(_envelope_payload(ENTRY_ENVELOPE), separators=(",", ":"))
    roll_payload = json.dumps(_envelope_payload(ROLL_ENVELOPE), separators=(",", ":"))
    entry_fingerprint = option_position_fingerprint(
        ((ENTRY_LONG, Decimal("1"), 100), (ENTRY_SHORT, Decimal("-1"), 100))
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            f"""
            INSERT INTO account_roles(role,account_fingerprint,equity,autonomous_enabled)
            VALUES ('DEVELOPMENT',repeat('a',64),100000,false);
            INSERT INTO alpaca_market_sessions(market_session_id,session_date,open_at,close_at,
              source_hash,session_hash,created_at) VALUES
              ('01000000-0000-0000-0000-000000000001','2026-08-29','2026-08-29 13:30+00',
               '2026-08-29 20:00+00',repeat('0',64),repeat('1',64),'2026-08-29 12:00+00'),
              ('01000000-0000-0000-0000-000000000002','2026-08-30','2026-08-30 13:30+00',
               '2026-08-30 20:00+00',repeat('2',64),repeat('3',64),'2026-08-30 12:00+00');
            INSERT INTO thesis_versions(thesis_version_id,thesis_id,account_role,version,
              origin_hash,thesis_hash,policy_hash,underlying,thesis_code,frozen_at,target_at,
              intended_exposure,exposure_limits,
              volatility_view,entry_atm_iv,approved_max_loss,portfolio_risk_cap,invalidation_codes,
              thesis_payload,created_at) VALUES
              ('10000000-0000-0000-0000-000000000001','10000000-0000-0000-0000-000000000002',
               'DEVELOPMENT',1,repeat('c',64),repeat('1',64),repeat('b',64),'NVDA','CATALYST_CONTINUATION',
               '2026-08-29 00:00+00','2026-09-04 00:00+00','{{}}','{{}}','LONG',0.4,500,500,
               '["GUIDANCE_REVERSED"]','{{}}','2026-08-29 00:00+00');
            INSERT INTO entry_approval_certificates(approval_id,account_role,policy_hash,
              book_fingerprint,envelope_hash,approved_max_loss,quantity,valid_from,expires_at,valid,
              thesis_version_id) VALUES ('20000000-0000-0000-0000-000000000001','DEVELOPMENT',
              repeat('b',64),repeat('2',64),'{order_envelope_hash(ENTRY_ENVELOPE)}',500,1,
              '2026-08-29 00:00+00',
              '2026-09-04 00:00+00',true,'10000000-0000-0000-0000-000000000001');
            INSERT INTO assessment_certificates(certificate_id,assessment_id,account_role,action,
              position_fingerprint,envelope_hash,approved_max_loss,quantity,policy_hash,created_at,
              expires_at,valid,thesis_version_id) VALUES
              ('30000000-0000-0000-0000-000000000001','30000000-0000-0000-0000-000000000002',
               'DEVELOPMENT','ROLL','{entry_fingerprint}','{order_envelope_hash(ROLL_ENVELOPE)}',500,1,repeat('b',64),
               '2026-08-30 14:00+00','2026-08-30 16:00+00',true,'{thesis_id}');
            INSERT INTO execution_intents(intent_id,account_role,intent_digest,action,policy_hash,
              event_key,trading_day,entry_approval_id,fingerprint,envelope_hash,envelope_payload,
              legs,quantity,minimum_limit,maximum_limit,approved_max_loss,state,first_fill_consumed)
              VALUES ('40000000-0000-0000-0000-000000000001','DEVELOPMENT',
              '{intent_digest(ENTRY_ENVELOPE)}','ENTRY',repeat('b',64),'ENTRY-1','2026-08-29',
              '20000000-0000-0000-0000-000000000001',repeat('2',64),
              '{order_envelope_hash(ENTRY_ENVELOPE)}','{entry_payload}','{entry_legs}',1,1,2,500,
              'TERMINAL',true);
            INSERT INTO execution_intents(intent_id,account_role,intent_digest,action,policy_hash,
              event_key,trading_day,assessment_certificate_id,fingerprint,envelope_hash,
              envelope_payload,legs,quantity,minimum_limit,maximum_limit,approved_max_loss,state,
              first_fill_consumed,market_session_id,quoted_relative_spread,
              maximum_relative_spread,incremental_debit,maximum_incremental_debit)
              VALUES ('40000000-0000-0000-0000-000000000002','DEVELOPMENT',
              '{intent_digest(ROLL_ENVELOPE)}','ROLL',repeat('b',64),'ROLL-1','2026-08-30',
              '30000000-0000-0000-0000-000000000001','{entry_fingerprint}',
              '{order_envelope_hash(ROLL_ENVELOPE)}','{roll_payload}','{roll_legs}',1,1,2,500,
              'TERMINAL',true,'{ROLL_ENVELOPE.market_session_id}',
              {ROLL_ENVELOPE.quoted_relative_spread},{ROLL_ENVELOPE.maximum_relative_spread},
              {ROLL_ENVELOPE.incremental_debit},{ROLL_ENVELOPE.maximum_incremental_debit});
            INSERT INTO order_attempts(attempt_id,execution_intent_id,attempt_ordinal,
              client_order_id,provider_order_id,state,request_hash,filled_quantity,quantity,
              filled_cash_flow) VALUES ('50000000-0000-0000-0000-000000000000',
              '40000000-0000-0000-0000-000000000001',0,'client-entry','provider-entry','FILLED',
              repeat('9',64),1,1,-500);
            INSERT INTO order_attempts(attempt_id,execution_intent_id,attempt_ordinal,
              client_order_id,
              provider_order_id,state,request_hash,filled_quantity,quantity,filled_cash_flow)
              VALUES ('50000000-0000-0000-0000-000000000001',
              '40000000-0000-0000-0000-000000000002',0,'client-roll','provider-roll','FILLED',
              repeat('5',64),1,1,10);
            INSERT INTO whole_account_reconciliations(reconciliation_id,reconciliation_hash,
              expectation_hash,execution_intent_id,intent_digest,account_role,account_fingerprint,
              purpose,attempt_ordinal,request_hash,accepted_at,expectation_payload,sweep_payload,
              positions_manifest_hash,orders_manifest_hash,activities_manifest_hash,safe,block_codes)
              VALUES ('60000000-0000-0000-0000-000000000002',repeat('ab',32),repeat('7',64),
              '40000000-0000-0000-0000-000000000002','{intent_digest(ROLL_ENVELOPE)}','DEVELOPMENT',
              '{reconciliation_fingerprint}','REPLACE',0,repeat('5',64),
              '2026-08-30 15:00+00','{{"purpose":"REPLACE","intent_id":
              "40000000-0000-0000-0000-000000000002","intent_digest":
              "{intent_digest(ROLL_ENVELOPE)}",
              "attempt_ordinal":0,"request_hash":
              "5555555555555555555555555555555555555555555555555555555555555555",
              "expected_cash":10,"expected_open_orders":[]}}',
              '{{"final_positions":{roll_inventory},"activities":{all_roll_activities},"retrieval_started_at":
              "2026-08-30T15:00:00+00:00","retrieval_completed_at":
              "2026-08-30T15:00:00+00:00"}}',repeat('8',64),repeat('9',64),repeat('d',64),true,'[]');
            INSERT INTO whole_account_reconciliations(reconciliation_id,reconciliation_hash,
              expectation_hash,execution_intent_id,intent_digest,account_role,account_fingerprint,
              purpose,attempt_ordinal,request_hash,accepted_at,expectation_payload,sweep_payload,
              positions_manifest_hash,orders_manifest_hash,activities_manifest_hash,safe,block_codes)
              VALUES ('60000000-0000-0000-0000-000000000001',repeat('a',64),repeat('7',64),
              '40000000-0000-0000-0000-000000000001','{intent_digest(ENTRY_ENVELOPE)}','DEVELOPMENT',repeat('a',64),
              'SUBMIT',0,repeat('9',64),'2026-08-29 15:00+00',
              '{{"purpose":"SUBMIT","intent_id":"40000000-0000-0000-0000-000000000001",
              "intent_digest":"{intent_digest(ENTRY_ENVELOPE)}","attempt_ordinal":0,
              "request_hash":"{"9" * 64}","expected_cash":-500,"expected_open_orders":[]}}',
              '{{"final_positions":{entry_inventory},"activities":{entry_activities},
              "retrieval_started_at":"2026-08-29T15:00:00+00:00",
              "retrieval_completed_at":"2026-08-29T15:00:00+00:00"}}',repeat('1',64),repeat('2',64),
              repeat('3',64),true,'[]');
            INSERT INTO execution_certificates(certificate_id,execution_intent_id,
              entry_approval_id,execution_status,attempt_ids,reconciliation_checks,created_at,
              reconciliation_id,reconciliation_hash) VALUES
              ('70000000-0000-0000-0000-000000000001','40000000-0000-0000-0000-000000000001',
               '20000000-0000-0000-0000-000000000001','FILLED','["client-entry"]','[]',
               '2026-08-29 15:00+00','60000000-0000-0000-0000-000000000001',repeat('a',64));
            INSERT INTO execution_certificates(certificate_id,execution_intent_id,
              assessment_certificate_id,execution_status,attempt_ids,reconciliation_checks,
              created_at,reconciliation_id,reconciliation_hash) VALUES
              ('70000000-0000-0000-0000-000000000002','40000000-0000-0000-0000-000000000002',
               '30000000-0000-0000-0000-000000000001','FILLED',
               '["client-roll"]','[]','2026-08-30 15:00+00',
               '60000000-0000-0000-0000-000000000002',repeat('ab',32));
            INSERT INTO broker_mutation_permits(permit_id,reconciliation_id,execution_intent_id,
              intent_digest,claim_token,claim_generation,execution_epoch,mutation_kind,
              attempt_ordinal,permit_generation,request_hash,limit_price,issued_at,expires_at,
              state,dispatch_nonce,dispatch_acquired_at,consumed_at,outcome_hash) VALUES
              ('92000000-0000-0000-0000-000000000002','60000000-0000-0000-0000-000000000002',
               '40000000-0000-0000-0000-000000000002','{intent_digest(ROLL_ENVELOPE)}',
               '94000000-0000-0000-0000-000000000002',1,0,'REPLACE',0,1,repeat('5',64),1,
               '2026-08-30 14:59+00','2026-08-30 15:01+00','CONSUMED',
               '95000000-0000-0000-0000-000000000002','2026-08-30 14:59+00',
               '2026-08-30 15:00+00',repeat('ab',32));
            UPDATE order_attempts SET broker_permit_id='92000000-0000-0000-0000-000000000002'
             WHERE attempt_id='50000000-0000-0000-0000-000000000001';
            INSERT INTO attempt_observations(observation_id,permit_id,execution_intent_id,
              attempt_id,attempt_ordinal,observation_sequence,source,provider_present,
              observed_payload,observed_at,observation_hash) VALUES
              ('93000000-0000-0000-0000-000000000002','92000000-0000-0000-0000-000000000002',
               '40000000-0000-0000-0000-000000000002','50000000-0000-0000-0000-000000000001',
               0,1,'DISPATCH_OUTCOME',true,'{{"intent_id":
               "40000000-0000-0000-0000-000000000002","ordinal":0,
               "client_order_id":"client-roll","request_hash":
               "5555555555555555555555555555555555555555555555555555555555555555",
               "state":"FILLED","replaces_client_order_id":null,
               "provider_order_id":"provider-roll","filled_quantity":1,"quantity":1,
               "fill_cash_flow":10}}','2026-08-30 15:00+00',repeat('e',64));
            INSERT INTO account_reconciliation_states(state_id,account_role,sequence,
              account_fingerprint,baseline_id,baseline_captured_at,accepted_at,expected_cash,
              expected_positions,expected_open_orders,known_activities,activity_complete_through,
              resolved_activity_hashes,state_hash) VALUES
              ('90000000-0000-0000-0000-000000000001','DEVELOPMENT',1,repeat('a',64),
               '91000000-0000-0000-0000-000000000001','2026-08-29 00:00+00',
               '2026-08-29 15:00+00',-500,'{entry_inventory}','[]','{entry_activities}',
               '2026-08-29 15:00+00','["{"a" * 64}","{"b" * 64}"]',repeat('d',64));
            INSERT INTO managed_lifecycle_positions(managed_position_id,account_role,
              account_fingerprint,entry_execution_certificate_id,entry_intent_id,entry_approval_id,
              thesis_version_id,entry_reconciliation_id,current_reconciliation_state_id,
              current_snapshot_id,active_position_fingerprint,activated_at) VALUES
              ('80000000-0000-0000-0000-000000000001','DEVELOPMENT',repeat('a',64),
               '70000000-0000-0000-0000-000000000001','40000000-0000-0000-0000-000000000001',
               '20000000-0000-0000-0000-000000000001','10000000-0000-0000-0000-000000000001',
               '60000000-0000-0000-0000-000000000001','90000000-0000-0000-0000-000000000001',
               'a0000000-0000-0000-0000-000000000001','{entry_fingerprint}','2026-08-29 15:00+00');
            INSERT INTO managed_position_transitions(transition_id,managed_position_id,
              transition_sequence,action,execution_intent_id,execution_certificate_id,
              post_reconciliation_id,fill_activity_manifest,fill_activity_manifest_hash,
              cashflow_contribution,resulting_position_fingerprint,occurred_at,market_session_id,
              transition_hash) VALUES ('b0000000-0000-0000-0000-000000000001',
              '80000000-0000-0000-0000-000000000001',0,'ENTRY',
              '40000000-0000-0000-0000-000000000001','70000000-0000-0000-0000-000000000001',
              '60000000-0000-0000-0000-000000000001','{entry_activities}',
              lifecycle_json_hash('{entry_activities}'),-500,'{entry_fingerprint}',
              '2026-08-29 15:00+00','01000000-0000-0000-0000-000000000001',repeat('2',64));
            INSERT INTO managed_position_snapshots(snapshot_id,managed_position_id,transition_id,
              reconciliation_id,reconciliation_state_id,normalized_inventory,inventory_hash,
              activity_manifest,activity_manifest_hash,cumulative_cashflow,rolls_on_trading_day,
              market_session_id,position_fingerprint,accepted_at,snapshot_hash) VALUES
              ('a0000000-0000-0000-0000-000000000001','80000000-0000-0000-0000-000000000001',
               'b0000000-0000-0000-0000-000000000001','60000000-0000-0000-0000-000000000001',
               '90000000-0000-0000-0000-000000000001','{entry_inventory}',
               lifecycle_json_hash('{entry_inventory}'),'{entry_activities}',
               lifecycle_json_hash('{entry_activities}'),-500,0,
               '01000000-0000-0000-0000-000000000001','{entry_fingerprint}',
               '2026-08-29 15:00+00',repeat('3',64));
            INSERT INTO agent_input_snapshots(snapshot_id,account_role,account_fingerprint,
              decision_kind,decision_boundary,observed_at,normalized_payload,input_hash,created_at)
              VALUES ('c0000000-0000-0000-0000-000000000001','DEVELOPMENT',repeat('a',64),
              'ASSESSMENT','2026-08-30 15:00+00','2026-08-30 15:00+00',
              '{{"acquisition_manifest_hash":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}}',
              repeat('4',64),'2026-08-30 15:00+00');
            INSERT INTO greek_authority_versions(authority_id,version,effective_at,
              timestamp_contract_hash,units_contract_hash,authority_payload,authority_hash,created_at)
              VALUES ('d0000000-0000-0000-0000-000000000001',1,'2026-08-29 00:00+00',
              repeat('5',64),repeat('ab',32),'{{}}',repeat('7',64),'2026-08-29 00:00+00');
            """
        )
        connection.exec_driver_sql("SET session_replication_role = origin")


def _configure_close_authority(connection) -> None:
    close_digest = intent_digest(CLOSE_ENVELOPE)
    close_hash = order_envelope_hash(CLOSE_ENVELOPE)
    close_payload = json.dumps(_envelope_payload(CLOSE_ENVELOPE), separators=(",", ":"))
    close_legs = json.dumps(CLOSE_LEGS, separators=(",", ":"))
    close_activities = json.dumps(
        ENTRY_ACTIVITIES + ROLL_FILL_ACTIVITIES[:2], separators=(",", ":")
    )
    connection.execute(
        text(
            "UPDATE assessment_certificates SET action='CLOSE',envelope_hash=:envelope "
            "WHERE certificate_id='30000000-0000-0000-0000-000000000001'"
        ),
        {"envelope": close_hash},
    )
    connection.execute(
        text(
            "UPDATE execution_intents SET action='CLOSE',intent_digest=:digest,"
            "envelope_hash=:envelope,envelope_payload=CAST(:payload AS jsonb),"
            "legs=CAST(:legs AS jsonb),market_session_id=NULL,"
            "quoted_relative_spread=NULL,maximum_relative_spread=NULL,"
            "incremental_debit=NULL,maximum_incremental_debit=NULL "
            "WHERE intent_id='40000000-0000-0000-0000-000000000002'"
        ),
        {
            "digest": close_digest,
            "envelope": close_hash,
            "payload": close_payload,
            "legs": close_legs,
        },
    )
    connection.execute(
        text(
            "UPDATE whole_account_reconciliations SET intent_digest=:digest,"
            "expectation_payload=jsonb_set(expectation_payload,'{intent_digest}',"
            "to_jsonb(CAST(:digest AS text))),sweep_payload=jsonb_set("
            "jsonb_set(sweep_payload,'{final_positions}','[]'::jsonb),'{activities}',"
            "CAST(:activities AS jsonb)) "
            "WHERE reconciliation_id='60000000-0000-0000-0000-000000000002'"
        ),
        {"digest": close_digest, "activities": close_activities},
    )
    connection.execute(
        text(
            "UPDATE broker_mutation_permits SET intent_digest=:digest "
            "WHERE permit_id='92000000-0000-0000-0000-000000000002'"
        ),
        {"digest": close_digest},
    )


def _insert_roll_transition(connection) -> None:
    connection.exec_driver_sql(
        """
        INSERT INTO managed_position_transitions(transition_id,managed_position_id,
          predecessor_transition_id,transition_sequence,action,execution_intent_id,
          execution_certificate_id,post_reconciliation_id,fill_activity_manifest,
          fill_activity_manifest_hash,cashflow_contribution,resulting_position_fingerprint,
          occurred_at,market_session_id,transition_hash) VALUES
          ('b0000000-0000-0000-0000-000000000002','80000000-0000-0000-0000-000000000001',
           'b0000000-0000-0000-0000-000000000001',1,'ROLL',
           '40000000-0000-0000-0000-000000000002','70000000-0000-0000-0000-000000000002',
           '60000000-0000-0000-0000-000000000002','[]',repeat('0',64),0,repeat('0',64),
           '2026-08-30 15:00+00','01000000-0000-0000-0000-000000000002',repeat('0',64));
        """
    )


def _insert_close_transition(connection) -> None:
    connection.exec_driver_sql(
        """
        INSERT INTO managed_position_transitions(transition_id,managed_position_id,
          predecessor_transition_id,transition_sequence,action,execution_intent_id,
          execution_certificate_id,post_reconciliation_id,fill_activity_manifest,
          fill_activity_manifest_hash,cashflow_contribution,resulting_position_fingerprint,
          occurred_at,market_session_id,transition_hash) VALUES
          ('b0000000-0000-0000-0000-000000000002','80000000-0000-0000-0000-000000000001',
           'b0000000-0000-0000-0000-000000000001',1,'CLOSE',
           '40000000-0000-0000-0000-000000000002','70000000-0000-0000-0000-000000000002',
           '60000000-0000-0000-0000-000000000002','[]',repeat('0',64),0,repeat('0',64),
           '2026-08-30 15:00+00','01000000-0000-0000-0000-000000000002',repeat('0',64));
        """
    )


def _install_transition_derivation(
    engine,
    *,
    include_activity: bool = True,
    activity: str | None = None,
    cashflow: int = 10,
    final_positions: str | None = None,
) -> None:
    if final_positions is None:
        final_positions = json.dumps(ROLL_INVENTORY, separators=(",", ":"))
    if activity is None:
        activity = json.dumps(
            (
                ROLL_FILL_ACTIVITIES[:2]
                if include_activity and final_positions == "[]"
                else ROLL_FILL_ACTIVITIES
                if include_activity
                else []
            ),
            separators=(",", ":"),
        )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE FUNCTION test_normalize_transition() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              NEW.fill_activity_manifest := '{activity}';
              NEW.fill_activity_manifest_hash := lifecycle_json_hash(NEW.fill_activity_manifest);
              NEW.cashflow_contribution := {cashflow};
              NEW.resulting_position_fingerprint :=
                lifecycle_position_fingerprint('{final_positions}');
              NEW.occurred_at := '2026-08-30 15:00+00';
              NEW.transition_hash := lifecycle_json_hash(to_jsonb(NEW)-'transition_hash');
              RETURN NEW;
            END $$;
            CREATE TRIGGER test_normalize_transition BEFORE INSERT ON managed_position_transitions
            FOR EACH ROW EXECUTE FUNCTION test_normalize_transition();
            """
        )


def test_roll_transition_snapshot_and_current_pointer_advance_in_one_transaction(
    lifecycle_engine,
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    _install_transition_derivation(lifecycle_engine)
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            """
            INSERT INTO account_reconciliation_states(state_id,account_role,sequence,
              account_fingerprint,baseline_id,baseline_captured_at,accepted_at,expected_cash,
              expected_positions,expected_open_orders,known_activities,activity_complete_through,
              resolved_activity_hashes,predecessor_state_id,authority_reconciliation_id,
              authority_permit_id,authority_observation_id,authority_permit_request_hash,
              state_hash)
            SELECT '90000000-0000-0000-0000-000000000002','DEVELOPMENT',2,repeat('a',64),
              '91000000-0000-0000-0000-000000000001','2026-08-29 00:00+00',accepted_at,
              (expectation_payload->>'expected_cash')::numeric,sweep_payload->'final_positions',
              expectation_payload->'expected_open_orders',sweep_payload->'activities',accepted_at,
              '[]','90000000-0000-0000-0000-000000000001',reconciliation_id,
              '92000000-0000-0000-0000-000000000002',
              '93000000-0000-0000-0000-000000000002',repeat('5',64),repeat('e',64)
            FROM whole_account_reconciliations
            WHERE reconciliation_id='60000000-0000-0000-0000-000000000002';
            CREATE FUNCTION test_normalize_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              NEW.snapshot_hash := lifecycle_json_hash(to_jsonb(NEW)-'snapshot_hash');
              RETURN NEW;
            END $$;
            CREATE TRIGGER test_normalize_snapshot BEFORE INSERT ON managed_position_snapshots
            FOR EACH ROW EXECUTE FUNCTION test_normalize_snapshot();
            """
        )
        connection.exec_driver_sql("SET session_replication_role = origin")
    with lifecycle_engine.begin() as connection:
        _insert_roll_transition(connection)
        connection.exec_driver_sql(
            """
            INSERT INTO managed_position_snapshots(snapshot_id,managed_position_id,
              predecessor_snapshot_id,transition_id,reconciliation_id,reconciliation_state_id,
              normalized_inventory,inventory_hash,activity_manifest,activity_manifest_hash,
              cumulative_cashflow,rolls_on_trading_day,market_session_id,position_fingerprint,
              accepted_at,snapshot_hash)
            SELECT 'a0000000-0000-0000-0000-000000000002',
              '80000000-0000-0000-0000-000000000001',
              'a0000000-0000-0000-0000-000000000001',
              'b0000000-0000-0000-0000-000000000002',reconciliation_id,
              '90000000-0000-0000-0000-000000000002',sweep_payload->'final_positions',
              lifecycle_json_hash(sweep_payload->'final_positions'),sweep_payload->'activities',
              lifecycle_json_hash(sweep_payload->'activities'),-490,1,
              '01000000-0000-0000-0000-000000000002',
              lifecycle_position_fingerprint(sweep_payload->'final_positions'),accepted_at,repeat('0',64)
            FROM whole_account_reconciliations
            WHERE reconciliation_id='60000000-0000-0000-0000-000000000002';
            UPDATE managed_lifecycle_positions
               SET current_reconciliation_state_id='90000000-0000-0000-0000-000000000002',
                   current_snapshot_id='a0000000-0000-0000-0000-000000000002',
                   active_position_fingerprint=(SELECT position_fingerprint
                     FROM managed_position_snapshots
                    WHERE snapshot_id='a0000000-0000-0000-0000-000000000002')
             WHERE managed_position_id='80000000-0000-0000-0000-000000000001';
            """
        )


def test_roll_rejects_successor_snapshot_bound_to_alternate_reconciliation_state(
    lifecycle_engine,
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    _install_transition_derivation(lifecycle_engine)
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            """
            INSERT INTO account_reconciliation_states(state_id,account_role,sequence,
              account_fingerprint,baseline_id,baseline_captured_at,accepted_at,expected_cash,
              expected_positions,expected_open_orders,known_activities,activity_complete_through,
              resolved_activity_hashes,predecessor_state_id,authority_reconciliation_id,
              authority_permit_id,authority_observation_id,authority_permit_request_hash,
              state_hash)
            SELECT '90000000-0000-0000-0000-000000000002','DEVELOPMENT',2,repeat('a',64),
              '91000000-0000-0000-0000-000000000001','2026-08-29 00:00+00',accepted_at,
              (expectation_payload->>'expected_cash')::numeric,sweep_payload->'final_positions',
              expectation_payload->'expected_open_orders',sweep_payload->'activities',accepted_at,
              '[]','90000000-0000-0000-0000-000000000001',reconciliation_id,
              '92000000-0000-0000-0000-000000000002',
              '93000000-0000-0000-0000-000000000002',repeat('5',64),repeat('e',64)
            FROM whole_account_reconciliations
            WHERE reconciliation_id='60000000-0000-0000-0000-000000000001';
            CREATE FUNCTION test_normalize_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              NEW.snapshot_hash := lifecycle_json_hash(to_jsonb(NEW)-'snapshot_hash');
              RETURN NEW;
            END $$;
            CREATE TRIGGER test_normalize_snapshot BEFORE INSERT ON managed_position_snapshots
            FOR EACH ROW EXECUTE FUNCTION test_normalize_snapshot();
            """
        )
        connection.exec_driver_sql("SET session_replication_role = origin")
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_SNAPSHOT_DERIVATION_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)
        connection.exec_driver_sql(
            """
            INSERT INTO managed_position_snapshots(snapshot_id,managed_position_id,
              predecessor_snapshot_id,transition_id,reconciliation_id,reconciliation_state_id,
              normalized_inventory,inventory_hash,activity_manifest,activity_manifest_hash,
              cumulative_cashflow,rolls_on_trading_day,market_session_id,position_fingerprint,
              accepted_at,snapshot_hash)
            SELECT 'a0000000-0000-0000-0000-000000000002',
              '80000000-0000-0000-0000-000000000001',
              'a0000000-0000-0000-0000-000000000001',
              'b0000000-0000-0000-0000-000000000002',reconciliation_id,
              '90000000-0000-0000-0000-000000000002',sweep_payload->'final_positions',
              lifecycle_json_hash(sweep_payload->'final_positions'),sweep_payload->'activities',
              lifecycle_json_hash(sweep_payload->'activities'),-490,1,
              '01000000-0000-0000-0000-000000000002',
              lifecycle_position_fingerprint(sweep_payload->'final_positions'),accepted_at,repeat('0',64)
            FROM whole_account_reconciliations
            WHERE reconciliation_id='60000000-0000-0000-0000-000000000002';
            UPDATE managed_lifecycle_positions
               SET current_reconciliation_state_id='90000000-0000-0000-0000-000000000002',
                   current_snapshot_id='a0000000-0000-0000-0000-000000000002',
                   active_position_fingerprint=(SELECT position_fingerprint
                     FROM managed_position_snapshots
                    WHERE snapshot_id='a0000000-0000-0000-0000-000000000002')
             WHERE managed_position_id='80000000-0000-0000-0000-000000000001';
            """
        )


def test_close_transition_snapshot_and_current_pointer_advance_in_one_transaction(
    lifecycle_engine,
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        _configure_close_authority(connection)
        connection.exec_driver_sql(
            """
            INSERT INTO account_reconciliation_states(state_id,account_role,sequence,
              account_fingerprint,baseline_id,baseline_captured_at,accepted_at,expected_cash,
              expected_positions,expected_open_orders,known_activities,activity_complete_through,
              resolved_activity_hashes,predecessor_state_id,authority_reconciliation_id,
              authority_permit_id,authority_observation_id,authority_permit_request_hash,
              state_hash)
            SELECT '90000000-0000-0000-0000-000000000002','DEVELOPMENT',2,repeat('a',64),
              '91000000-0000-0000-0000-000000000001','2026-08-29 00:00+00',accepted_at,
              (expectation_payload->>'expected_cash')::numeric,sweep_payload->'final_positions',
              expectation_payload->'expected_open_orders',sweep_payload->'activities',accepted_at,
              '[]','90000000-0000-0000-0000-000000000001',reconciliation_id,
              '92000000-0000-0000-0000-000000000002',
              '93000000-0000-0000-0000-000000000002',repeat('5',64),repeat('e',64)
            FROM whole_account_reconciliations
            WHERE reconciliation_id='60000000-0000-0000-0000-000000000002';
            CREATE FUNCTION test_normalize_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              NEW.snapshot_hash := lifecycle_json_hash(to_jsonb(NEW)-'snapshot_hash');
              RETURN NEW;
            END $$;
            CREATE TRIGGER test_normalize_snapshot BEFORE INSERT ON managed_position_snapshots
            FOR EACH ROW EXECUTE FUNCTION test_normalize_snapshot();
            """
        )
        connection.exec_driver_sql("SET session_replication_role = origin")
    _install_transition_derivation(lifecycle_engine, final_positions="[]")
    with lifecycle_engine.begin() as connection:
        _insert_close_transition(connection)
        connection.exec_driver_sql(
            """
            INSERT INTO managed_position_snapshots(snapshot_id,managed_position_id,
              predecessor_snapshot_id,transition_id,reconciliation_id,reconciliation_state_id,
              normalized_inventory,inventory_hash,activity_manifest,activity_manifest_hash,
              cumulative_cashflow,rolls_on_trading_day,market_session_id,position_fingerprint,
              accepted_at,snapshot_hash)
            SELECT 'a0000000-0000-0000-0000-000000000002',
              '80000000-0000-0000-0000-000000000001',
              'a0000000-0000-0000-0000-000000000001',
              'b0000000-0000-0000-0000-000000000002',reconciliation_id,
              '90000000-0000-0000-0000-000000000002',sweep_payload->'final_positions',
              lifecycle_json_hash(sweep_payload->'final_positions'),sweep_payload->'activities',
              lifecycle_json_hash(sweep_payload->'activities'),-490,0,
              '01000000-0000-0000-0000-000000000002',
              lifecycle_position_fingerprint(sweep_payload->'final_positions'),accepted_at,repeat('0',64)
            FROM whole_account_reconciliations
            WHERE reconciliation_id='60000000-0000-0000-0000-000000000002';
            UPDATE managed_lifecycle_positions
               SET current_reconciliation_state_id='90000000-0000-0000-0000-000000000002',
                   current_snapshot_id='a0000000-0000-0000-0000-000000000002',
                   active_position_fingerprint=(SELECT position_fingerprint
                     FROM managed_position_snapshots
                    WHERE snapshot_id='a0000000-0000-0000-0000-000000000002'),
                   closed_at='2026-08-30 15:00+00'
             WHERE managed_position_id='80000000-0000-0000-0000-000000000001';
            """
        )


def test_transition_rejects_missing_successor_pointer_advance(lifecycle_engine) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    _install_transition_derivation(lifecycle_engine)
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_TRANSITION_PREDECESSOR_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)


def test_transition_rejects_pointer_advance_before_successor_snapshot(lifecycle_engine) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    _install_transition_derivation(lifecycle_engine)
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_CURRENT_SNAPSHOT_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)
        connection.exec_driver_sql(
            """
            UPDATE managed_lifecycle_positions
               SET current_reconciliation_state_id='90000000-0000-0000-0000-000000000001',
                   current_snapshot_id='a0000000-0000-0000-0000-000000000002',
                   active_position_fingerprint=repeat('d',64)
             WHERE managed_position_id='80000000-0000-0000-0000-000000000001'
            """
        )


def test_transition_rejects_assessment_bound_to_successor_fingerprint(lifecycle_engine) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            "UPDATE assessment_certificates SET position_fingerprint="
            'lifecycle_json_hash(\'[{"kind":"OPTION","symbol":"NVDA260918C00170000",'
            '"signed_quantity":"1","multiplier":100}]\') '
            "WHERE certificate_id='30000000-0000-0000-0000-000000000001'"
        )
        connection.exec_driver_sql("SET session_replication_role = origin")
    _install_transition_derivation(lifecycle_engine)
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_TRANSITION_PREDECESSOR_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)


def _exercise_reconciliation_state_substitution(
    engine,
    *,
    action: str,
    state_mutation: str = "",
    pointer_state_id: str = "90000000-0000-0000-0000-000000000002",
) -> None:
    _seed_transition_authority(
        engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    final_positions = (
        "[]" if action == "CLOSE" else json.dumps(ROLL_INVENTORY, separators=(",", ":"))
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        if action == "CLOSE":
            _configure_close_authority(connection)
        connection.exec_driver_sql(
            """
            INSERT INTO account_reconciliation_states(state_id,account_role,sequence,
              account_fingerprint,baseline_id,baseline_captured_at,accepted_at,expected_cash,
              expected_positions,expected_open_orders,known_activities,activity_complete_through,
              resolved_activity_hashes,predecessor_state_id,authority_reconciliation_id,
              authority_permit_id,authority_observation_id,authority_permit_request_hash,state_hash)
            SELECT '90000000-0000-0000-0000-000000000002','DEVELOPMENT',2,repeat('a',64),
              '91000000-0000-0000-0000-000000000001','2026-08-29 00:00+00',accepted_at,10,
              sweep_payload->'final_positions','[]',sweep_payload->'activities',accepted_at,'[]',
              '90000000-0000-0000-0000-000000000001',reconciliation_id,
              '92000000-0000-0000-0000-000000000002',
              '93000000-0000-0000-0000-000000000002',repeat('5',64),repeat('e',64)
            FROM whole_account_reconciliations
            WHERE reconciliation_id='60000000-0000-0000-0000-000000000002';
            CREATE FUNCTION test_normalize_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              NEW.snapshot_hash := lifecycle_json_hash(to_jsonb(NEW)-'snapshot_hash');
              RETURN NEW;
            END $$;
            CREATE TRIGGER test_normalize_snapshot BEFORE INSERT ON managed_position_snapshots
            FOR EACH ROW EXECUTE FUNCTION test_normalize_snapshot();
            """
        )
        if state_mutation:
            connection.exec_driver_sql(state_mutation)
        connection.exec_driver_sql("SET session_replication_role = origin")
    _install_transition_derivation(engine, final_positions=final_positions)
    with engine.begin() as connection:
        if action == "CLOSE":
            _insert_close_transition(connection)
        else:
            _insert_roll_transition(connection)
        connection.exec_driver_sql(
            f"""
            INSERT INTO managed_position_snapshots(snapshot_id,managed_position_id,
              predecessor_snapshot_id,transition_id,reconciliation_id,reconciliation_state_id,
              normalized_inventory,inventory_hash,activity_manifest,activity_manifest_hash,
              cumulative_cashflow,rolls_on_trading_day,market_session_id,position_fingerprint,
              accepted_at,snapshot_hash)
            SELECT 'a0000000-0000-0000-0000-000000000002',
              '80000000-0000-0000-0000-000000000001',
              'a0000000-0000-0000-0000-000000000001',
              'b0000000-0000-0000-0000-000000000002',reconciliation_id,
              '90000000-0000-0000-0000-000000000002',sweep_payload->'final_positions',
              lifecycle_json_hash(sweep_payload->'final_positions'),sweep_payload->'activities',
              lifecycle_json_hash(sweep_payload->'activities'),-490,
              {0 if action == "CLOSE" else 1},'01000000-0000-0000-0000-000000000002',
              lifecycle_position_fingerprint(sweep_payload->'final_positions'),accepted_at,repeat('0',64)
            FROM whole_account_reconciliations
            WHERE reconciliation_id='60000000-0000-0000-0000-000000000002';
            UPDATE managed_lifecycle_positions
               SET current_reconciliation_state_id='{pointer_state_id}',
                   current_snapshot_id='a0000000-0000-0000-0000-000000000002',
                   active_position_fingerprint=(SELECT position_fingerprint
                     FROM managed_position_snapshots
                    WHERE snapshot_id='a0000000-0000-0000-0000-000000000002')
                   {",closed_at='2026-08-30 15:00+00'" if action == "CLOSE" else ""}
             WHERE managed_position_id='80000000-0000-0000-0000-000000000001';
            """
        )


@pytest.mark.parametrize("action", ("ROLL", "CLOSE"))
@pytest.mark.parametrize(
    ("case", "state_mutation", "pointer_state_id"),
    (
        (
            "stale",
            "UPDATE account_reconciliation_states SET accepted_at='2026-08-29 15:00+00' "
            "WHERE state_id='90000000-0000-0000-0000-000000000002'",
            "90000000-0000-0000-0000-000000000002",
        ),
        (
            "alternate-same-account",
            "UPDATE account_reconciliation_states SET authority_reconciliation_id="
            "'60000000-0000-0000-0000-000000000001' "
            "WHERE state_id='90000000-0000-0000-0000-000000000002'",
            "90000000-0000-0000-0000-000000000002",
        ),
        (
            "cross-account",
            "UPDATE account_reconciliation_states SET account_fingerprint=repeat('f',64) "
            "WHERE state_id='90000000-0000-0000-0000-000000000002'",
            "90000000-0000-0000-0000-000000000002",
        ),
        (
            "predecessor-state",
            "UPDATE account_reconciliation_states SET predecessor_state_id="
            "'90000000-0000-0000-0000-000000000099' "
            "WHERE state_id='90000000-0000-0000-0000-000000000002'",
            "90000000-0000-0000-0000-000000000002",
        ),
        ("current-state-pointer", "", "90000000-0000-0000-0000-000000000001"),
    ),
    ids=lambda value: value if isinstance(value, str) and " " not in value else None,
)
def test_roll_and_close_reject_reconciliation_state_substitution(
    lifecycle_engine,
    action: str,
    case: str,
    state_mutation: str,
    pointer_state_id: str,
) -> None:
    del case
    with pytest.raises(DBAPIError, match="MANAGED_POSITION_(SNAPSHOT|TRANSITION|CURRENT)"):
        _exercise_reconciliation_state_substitution(
            lifecycle_engine,
            action=action,
            state_mutation=state_mutation,
            pointer_state_id=pointer_state_id,
        )


@pytest.mark.parametrize("action", ("ROLL", "CLOSE"))
def test_roll_and_close_accept_account_cash_distinct_from_lifecycle_cashflow(
    lifecycle_engine,
    action: str,
) -> None:
    _exercise_reconciliation_state_substitution(
        lifecycle_engine,
        action=action,
        state_mutation="""
            UPDATE whole_account_reconciliations
               SET expectation_payload=jsonb_set(
                   expectation_payload,'{expected_cash}','100010'::jsonb)
             WHERE reconciliation_id='60000000-0000-0000-0000-000000000002';
            UPDATE account_reconciliation_states SET expected_cash=100010
             WHERE state_id='90000000-0000-0000-0000-000000000002';
        """,
    )


def test_attempt_observation_rejects_time_before_permit_dispatch(lifecycle_engine) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with (
        pytest.raises(DBAPIError, match="ATTEMPT_OBSERVATION_TIMING_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            """
            INSERT INTO attempt_observations(observation_id,permit_id,execution_intent_id,
              attempt_id,attempt_ordinal,observation_sequence,source,provider_present,
              observed_payload,observed_at,observation_hash)
            SELECT '93000000-0000-0000-0000-000000000003',permit_id,execution_intent_id,
              attempt_id,attempt_ordinal,2,'TARGETED_LOOKUP',provider_present,observed_payload,
              '2026-08-30 14:58+00',repeat('f',64)
            FROM attempt_observations
            WHERE observation_id='93000000-0000-0000-0000-000000000002'
            """
        )


def test_permit_consumption_rejects_time_after_existing_observation(lifecycle_engine) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            """
            DELETE FROM attempt_observations
             WHERE observation_id='93000000-0000-0000-0000-000000000002';
            UPDATE broker_mutation_permits
               SET state='DISPATCHING',consumed_at=NULL,outcome_hash=NULL
             WHERE permit_id='92000000-0000-0000-0000-000000000002'
            """
        )
        connection.exec_driver_sql("SET session_replication_role = origin")
    with (
        pytest.raises(DBAPIError, match="BROKER_PERMIT_CONSUMPTION_TIMING_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            """
            INSERT INTO attempt_observations(observation_id,permit_id,execution_intent_id,
              attempt_id,attempt_ordinal,observation_sequence,source,provider_present,
              observed_payload,observed_at,observation_hash)
            VALUES ('93000000-0000-0000-0000-000000000003',
              '92000000-0000-0000-0000-000000000002',
              '40000000-0000-0000-0000-000000000002',
              '50000000-0000-0000-0000-000000000001',0,1,'DISPATCH_OUTCOME',true,
              '{"intent_id":"40000000-0000-0000-0000-000000000002","ordinal":0,
              "client_order_id":"client-roll","request_hash":
              "5555555555555555555555555555555555555555555555555555555555555555",
              "state":"FILLED","replaces_client_order_id":null,
              "provider_order_id":"provider-roll","filled_quantity":1,"quantity":1,
              "fill_cash_flow":10}','2026-08-30 15:00+00',repeat('f',64));
            UPDATE broker_mutation_permits
               SET state='CONSUMED',consumed_at='2026-08-30 15:01+00',outcome_hash=repeat('f',64)
             WHERE permit_id='92000000-0000-0000-0000-000000000002';
            """
        )


def test_concurrent_observation_and_late_consumption_cannot_commit(lifecycle_engine) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            "DELETE FROM attempt_observations "
            "WHERE observation_id='93000000-0000-0000-0000-000000000002'; "
            "UPDATE broker_mutation_permits "
            "SET state='DISPATCHING',consumed_at=NULL,outcome_hash=NULL "
            "WHERE permit_id='92000000-0000-0000-0000-000000000002'"
        )
        connection.exec_driver_sql("SET session_replication_role = origin")

    observation_connection = lifecycle_engine.connect()
    observation_transaction = observation_connection.begin()
    try:
        observation_connection.exec_driver_sql(
            """
            INSERT INTO attempt_observations(observation_id,permit_id,execution_intent_id,
              attempt_id,attempt_ordinal,observation_sequence,source,provider_present,
              observed_payload,observed_at,observation_hash)
            VALUES ('93000000-0000-0000-0000-000000000003',
              '92000000-0000-0000-0000-000000000002',
              '40000000-0000-0000-0000-000000000002',
              '50000000-0000-0000-0000-000000000001',0,1,'DISPATCH_OUTCOME',true,
              '{"intent_id":"40000000-0000-0000-0000-000000000002","ordinal":0,
              "client_order_id":"client-roll","request_hash":
              "5555555555555555555555555555555555555555555555555555555555555555",
              "state":"FILLED","replaces_client_order_id":null,
              "provider_order_id":"provider-roll","filled_quantity":1,"quantity":1,
              "fill_cash_flow":10}','2026-08-30 15:00+00',repeat('f',64))
            """
        )

        def consume_late() -> None:
            with lifecycle_engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL lock_timeout='2s'")
                connection.exec_driver_sql(
                    "UPDATE broker_mutation_permits "
                    "SET state='CONSUMED',consumed_at='2026-08-30 15:01+00',"
                    "outcome_hash=repeat('f',64) "
                    "WHERE permit_id='92000000-0000-0000-0000-000000000002'"
                )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(consume_late)
            try:
                future.result(timeout=1)
            except FutureTimeout:
                observation_transaction.commit()
                with pytest.raises(
                    DBAPIError,
                    match="BROKER_PERMIT_CONSUMPTION_TIMING_INVALID",
                ):
                    future.result(timeout=3)
            except DBAPIError as error:
                assert "BROKER_PERMIT_CONSUMPTION_TIMING_INVALID" in str(error)
                observation_transaction.commit()
            else:
                observation_transaction.commit()
                with lifecycle_engine.connect() as connection:
                    consumed_at, observed_at = connection.exec_driver_sql(
                        "SELECT consumed_at,observed_at FROM broker_mutation_permits "
                        "JOIN attempt_observations USING (permit_id) "
                        "WHERE permit_id='92000000-0000-0000-0000-000000000002'"
                    ).one()
                assert consumed_at <= observed_at
    finally:
        if observation_transaction.is_active:
            observation_transaction.rollback()
        observation_connection.close()


def test_observation_trigger_does_not_reverse_repository_lock_order(lifecycle_engine) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            "DELETE FROM attempt_observations "
            "WHERE observation_id='93000000-0000-0000-0000-000000000002'; "
            "UPDATE broker_mutation_permits "
            "SET state='DISPATCHING',consumed_at=NULL,outcome_hash=NULL "
            "WHERE permit_id='92000000-0000-0000-0000-000000000002'"
        )
        connection.exec_driver_sql("SET session_replication_role = origin")

    repository_connection = lifecycle_engine.connect()
    repository_transaction = repository_connection.begin()
    insert_started = Event()
    insert_backend: list[int] = []
    try:
        repository_connection.exec_driver_sql("SET LOCAL deadlock_timeout='100ms'")
        repository_connection.exec_driver_sql("SET LOCAL statement_timeout='3s'")
        repository_connection.exec_driver_sql(
            "SELECT * FROM order_attempts "
            "WHERE attempt_id='50000000-0000-0000-0000-000000000001' FOR UPDATE"
        )

        def insert_observation() -> None:
            with lifecycle_engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL deadlock_timeout='100ms'")
                connection.exec_driver_sql("SET LOCAL statement_timeout='3s'")
                insert_backend.append(
                    connection.exec_driver_sql("SELECT pg_backend_pid()").scalar()
                )
                insert_started.set()
                connection.exec_driver_sql(
                    """
                    INSERT INTO attempt_observations(observation_id,permit_id,
                      execution_intent_id,attempt_id,attempt_ordinal,observation_sequence,
                      source,provider_present,observed_payload,observed_at,observation_hash)
                    VALUES ('93000000-0000-0000-0000-000000000003',
                      '92000000-0000-0000-0000-000000000002',
                      '40000000-0000-0000-0000-000000000002',
                      '50000000-0000-0000-0000-000000000001',0,1,'DISPATCH_OUTCOME',true,
                      '{"intent_id":"40000000-0000-0000-0000-000000000002","ordinal":0,
                      "client_order_id":"client-roll","request_hash":
                      "5555555555555555555555555555555555555555555555555555555555555555",
                      "state":"FILLED","replaces_client_order_id":null,
                      "provider_order_id":"provider-roll","filled_quantity":1,"quantity":1,
                      "fill_cash_flow":10}','2026-08-30 15:00+00',repeat('f',64))
                    """
                )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(insert_observation)
            assert insert_started.wait(timeout=1)
            for _ in range(100):
                with lifecycle_engine.connect() as monitor:
                    waiting = monitor.exec_driver_sql(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (insert_backend[0],),
                    ).scalar()
                if waiting == "Lock":
                    break
            else:
                pytest.fail("observation insert did not reach the locked attempt")
            repository_connection.exec_driver_sql(
                "SELECT * FROM broker_mutation_permits "
                "WHERE permit_id='92000000-0000-0000-0000-000000000002' FOR UPDATE"
            )
            repository_transaction.commit()
            future.result(timeout=3)
    finally:
        if repository_transaction.is_active:
            repository_transaction.rollback()
        repository_connection.close()


def test_reconciliation_state_trigger_does_not_reverse_repository_lock_order(
    lifecycle_engine,
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE account_reconciliation_states "
            "DISABLE TRIGGER account_reconciliation_state_insert_guard"
        )

    repository_connection = lifecycle_engine.connect()
    repository_transaction = repository_connection.begin()
    insert_started = Event()
    insert_backend: list[int] = []
    try:
        repository_connection.exec_driver_sql("SET LOCAL deadlock_timeout='100ms'")
        repository_connection.exec_driver_sql("SET LOCAL statement_timeout='3s'")
        repository_connection.exec_driver_sql(
            "SELECT * FROM order_attempts "
            "WHERE attempt_id='50000000-0000-0000-0000-000000000001' FOR UPDATE"
        )

        def insert_reconciliation_state() -> None:
            with lifecycle_engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL deadlock_timeout='100ms'")
                connection.exec_driver_sql("SET LOCAL statement_timeout='3s'")
                insert_backend.append(
                    connection.exec_driver_sql("SELECT pg_backend_pid()").scalar()
                )
                insert_started.set()
                connection.exec_driver_sql(
                    """
                    INSERT INTO account_reconciliation_states(state_id,account_role,sequence,
                      account_fingerprint,baseline_id,baseline_captured_at,accepted_at,
                      expected_cash,expected_positions,expected_open_orders,known_activities,
                      activity_complete_through,resolved_activity_hashes,predecessor_state_id,
                      authority_reconciliation_id,authority_permit_id,authority_observation_id,
                      authority_permit_request_hash,state_hash)
                    SELECT '90000000-0000-0000-0000-000000000099','DEVELOPMENT',2,
                      repeat('a',64),'91000000-0000-0000-0000-000000000001',
                      '2026-08-29 00:00+00',accepted_at,10,sweep_payload->'final_positions',
                      '[]',sweep_payload->'activities',accepted_at,'[]',
                      '90000000-0000-0000-0000-000000000001',reconciliation_id,
                      '92000000-0000-0000-0000-000000000002',
                      '93000000-0000-0000-0000-000000000002',repeat('5',64),repeat('e',64)
                    FROM whole_account_reconciliations
                    WHERE reconciliation_id='60000000-0000-0000-0000-000000000002'
                    """
                )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(insert_reconciliation_state)
            assert insert_started.wait(timeout=1)
            for _ in range(100):
                if future.done():
                    future.result()
                with lifecycle_engine.connect() as monitor:
                    waiting = monitor.exec_driver_sql(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (insert_backend[0],),
                    ).scalar()
                if waiting == "Lock":
                    break
            else:
                pytest.fail("reconciliation state insert did not reach the locked attempt")
            repository_connection.exec_driver_sql(
                "SELECT * FROM broker_mutation_permits "
                "WHERE permit_id='92000000-0000-0000-0000-000000000002' FOR UPDATE"
            )
            repository_transaction.commit()
            with pytest.raises(DBAPIError) as rejected:
                future.result(timeout=3)
            assert "deadlock detected" not in str(rejected.value)
    finally:
        if repository_transaction.is_active:
            repository_transaction.rollback()
        repository_connection.close()


def test_reconciliation_rejects_sweep_completion_before_start(lifecycle_engine) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with (
        pytest.raises(DBAPIError, match="RECONCILIATION_TIMING_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            """
            INSERT INTO whole_account_reconciliations(reconciliation_id,reconciliation_hash,
              expectation_hash,execution_intent_id,intent_digest,account_role,account_fingerprint,
              purpose,attempt_ordinal,request_hash,accepted_at,expectation_payload,sweep_payload,
              positions_manifest_hash,orders_manifest_hash,activities_manifest_hash,safe,block_codes)
            SELECT '60000000-0000-0000-0000-000000000003',repeat('f',64),expectation_hash,
              execution_intent_id,intent_digest,account_role,account_fingerprint,purpose,
              attempt_ordinal,request_hash,accepted_at,expectation_payload,
              jsonb_set(sweep_payload,'{retrieval_completed_at}',
                '"2026-08-30T14:59:00+00:00"'::jsonb),positions_manifest_hash,
              orders_manifest_hash,activities_manifest_hash,safe,block_codes
            FROM whole_account_reconciliations
            WHERE reconciliation_id='60000000-0000-0000-0000-000000000002'
            """
        )


@pytest.mark.parametrize("action", ("ROLL", "CLOSE"))
@pytest.mark.parametrize(
    "state_mutation",
    (
        """
            UPDATE broker_mutation_permits SET request_hash=repeat('f',64)
             WHERE permit_id='92000000-0000-0000-0000-000000000002';
            UPDATE account_reconciliation_states SET authority_permit_request_hash=repeat('f',64)
             WHERE state_id='90000000-0000-0000-0000-000000000002';
        """,
        "UPDATE broker_mutation_permits SET attempt_ordinal=1 "
        "WHERE permit_id='92000000-0000-0000-0000-000000000002'",
        "UPDATE attempt_observations SET attempt_ordinal=1 "
        "WHERE observation_id='93000000-0000-0000-0000-000000000002'",
        "UPDATE attempt_observations SET provider_present=false,observed_payload=NULL "
        "WHERE observation_id='93000000-0000-0000-0000-000000000002'",
        "UPDATE attempt_observations SET observed_at='2026-08-30 16:00+00' "
        "WHERE observation_id='93000000-0000-0000-0000-000000000002'",
        "UPDATE attempt_observations SET observed_at='2026-08-30 14:58+00' "
        "WHERE observation_id='93000000-0000-0000-0000-000000000002'",
        "UPDATE broker_mutation_permits SET consumed_at='2026-08-30 15:01+00' "
        "WHERE permit_id='92000000-0000-0000-0000-000000000002'",
        "UPDATE whole_account_reconciliations SET sweep_payload=jsonb_set(sweep_payload, "
        "'{retrieval_completed_at}', '\"2026-08-30T14:59:00+00:00\"'::jsonb) "
        "WHERE reconciliation_id='60000000-0000-0000-0000-000000000002'",
        "UPDATE order_attempts SET request_hash=repeat('f',64) "
        "WHERE attempt_id='50000000-0000-0000-0000-000000000001'",
        """
            INSERT INTO order_attempts(attempt_id,execution_intent_id,attempt_ordinal,
              client_order_id,provider_order_id,state,request_hash,filled_quantity,quantity,
              filled_cash_flow) VALUES
              ('50000000-0000-0000-0000-000000000099',
               '40000000-0000-0000-0000-000000000002',1,'client-wrong','provider-wrong',
               'CANCELED',repeat('f',64),0,1,0);
            UPDATE order_attempts SET broker_permit_id=NULL
             WHERE attempt_id='50000000-0000-0000-0000-000000000001';
                UPDATE order_attempts SET broker_permit_id='92000000-0000-0000-0000-000000000002'
                 WHERE attempt_id='50000000-0000-0000-0000-000000000099';
                UPDATE execution_certificates SET attempt_ids='["client-roll","client-wrong"]'
                 WHERE certificate_id='70000000-0000-0000-0000-000000000002';
                UPDATE attempt_observations
               SET attempt_id='50000000-0000-0000-0000-000000000099',attempt_ordinal=1
             WHERE observation_id='93000000-0000-0000-0000-000000000002';
        """,
    ),
    ids=(
        "permit-request",
        "permit-ordinal",
        "observation-ordinal",
        "observation-absence",
        "observation-after-reconciliation",
        "observation-before-permit",
        "permit-consumed-after-observation",
        "sweep-completes-before-start",
        "attempt-request",
        "attempt-substitution",
    ),
)
def test_roll_and_close_reject_detached_reconciliation_state_authority(
    lifecycle_engine,
    action: str,
    state_mutation: str,
) -> None:
    with pytest.raises(DBAPIError, match="MANAGED_POSITION_SNAPSHOT_DERIVATION_INVALID"):
        _exercise_reconciliation_state_substitution(
            lifecycle_engine,
            action=action,
            state_mutation=state_mutation,
        )


@pytest.mark.parametrize(
    ("fingerprint", "thesis_id"),
    (
        ("f" * 64, "10000000-0000-0000-0000-000000000001"),
        ("a" * 64, "10000000-0000-0000-0000-000000000099"),
    ),
)
def test_transition_rejects_cross_account_or_foreign_thesis(
    lifecycle_engine, fingerprint: str, thesis_id: str
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint=fingerprint,
        thesis_id=thesis_id,
    )
    _install_transition_derivation(lifecycle_engine)
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_TRANSITION"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)


def test_transition_rejects_caller_selected_hashes(lifecycle_engine) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_TRANSITION_LINEAGE_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)


@pytest.mark.parametrize(
    "attempt_ids",
    (
        '["50000000-0000-0000-0000-000000000001"]',
        '["client-roll","client-roll"]',
        '["foreign-client"]',
        "[1]",
    ),
    ids=("attempt-uuid", "duplicate", "foreign-client", "non-string"),
)
def test_transition_rejects_noncanonical_certificate_attempt_lineage(
    lifecycle_engine, attempt_ids: str
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.execute(
            text(
                "UPDATE execution_certificates SET attempt_ids=CAST(:attempt_ids AS jsonb) "
                "WHERE certificate_id='70000000-0000-0000-0000-000000000002'"
            ),
            {"attempt_ids": attempt_ids},
        )
        connection.exec_driver_sql("SET session_replication_role = origin")
    _install_transition_derivation(lifecycle_engine)
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_TRANSITION_LINEAGE_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)


def test_transition_requires_exact_fill_activity_for_every_filled_attempt(
    lifecycle_engine,
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            "UPDATE whole_account_reconciliations SET sweep_payload="
            '\'{"final_positions":[{"kind":"OPTION","symbol":'
            '"NVDA260918C00170000","signed_quantity":"1","multiplier":100}],'
            '"activities":[]}\' WHERE reconciliation_id='
            "'60000000-0000-0000-0000-000000000002'"
        )
        connection.exec_driver_sql("SET session_replication_role = origin")
    _install_transition_derivation(lifecycle_engine, include_activity=False)
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_TRANSITION_LINEAGE_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)


def test_transition_rejects_extra_fill_activity_without_a_filled_attempt(
    lifecycle_engine,
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            "UPDATE whole_account_reconciliations SET sweep_payload="
            "jsonb_set(sweep_payload, '{activities}', "
            "(sweep_payload->'activities') || "
            '\'[{"activity_id_hash":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
            '"activity_type":"FILL","provider_order_id":"provider-extra",'
            '"client_order_id":"client-extra"}]\'::jsonb) '
            "WHERE reconciliation_id='60000000-0000-0000-0000-000000000002'"
        )
        connection.exec_driver_sql("SET session_replication_role = origin")
    _install_transition_derivation(lifecycle_engine)
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_TRANSITION_LINEAGE_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)


def test_transition_rejects_one_activity_cross_paired_to_two_filled_attempts(
    lifecycle_engine,
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    cross_paired_activity = (
        '[{"activity_id_hash":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",'
        '"activity_type":"FILL","provider_order_id":"provider-roll",'
        '"client_order_id":"client-second"}]'
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            """
            INSERT INTO order_attempts(attempt_id,execution_intent_id,attempt_ordinal,
              client_order_id,provider_order_id,state,request_hash,filled_quantity,quantity,
              filled_cash_flow) VALUES
              ('50000000-0000-0000-0000-000000000002',
               '40000000-0000-0000-0000-000000000002',1,'client-second','provider-second',
               'FILLED',repeat('4',64),1,1,5);
            UPDATE execution_certificates
               SET attempt_ids='["client-roll","client-second"]'::jsonb
             WHERE certificate_id='70000000-0000-0000-0000-000000000002';
            UPDATE whole_account_reconciliations
               SET sweep_payload=jsonb_set(
                   sweep_payload,
                   '{activities}',
                   '[{"activity_id_hash":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                      "activity_type":"FILL","provider_order_id":"provider-roll",
                      "client_order_id":"client-second"}]'::jsonb
               )
             WHERE reconciliation_id='60000000-0000-0000-0000-000000000002';
            """
        )
        connection.exec_driver_sql("SET session_replication_role = origin")
    _install_transition_derivation(
        lifecycle_engine,
        activity=cross_paired_activity,
        cashflow=15,
    )
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_TRANSITION_LINEAGE_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)


@pytest.mark.parametrize(
    "activities",
    (
        (
            '[{"activity_id_hash":"1111111111111111111111111111111111111111111111111111111111111111",'
            '"activity_type":"FILL","provider_order_id":"provider-roll",'
            '"client_order_id":"client-roll"},'
            '{"activity_id_hash":"2222222222222222222222222222222222222222222222222222222222222222",'
            '"activity_type":"FILL","provider_order_id":"provider-roll",'
            '"client_order_id":"client-roll"}]'
        ),
        (
            '[{"activity_id_hash":"3333333333333333333333333333333333333333333333333333333333333333",'
            '"activity_type":"FILL","provider_order_id":"provider-roll"}]'
        ),
    ),
    ids=("duplicate", "missing-client-identity"),
)
def test_transition_rejects_ambiguous_or_incomplete_fill_identity(
    lifecycle_engine,
    activities: str,
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.execute(
            text(
                "UPDATE whole_account_reconciliations SET sweep_payload="
                "jsonb_set(sweep_payload, '{activities}', CAST(:activities AS jsonb)) "
                "WHERE reconciliation_id='60000000-0000-0000-0000-000000000002'"
            ),
            {"activities": activities},
        )
        connection.exec_driver_sql("SET session_replication_role = origin")
    _install_transition_derivation(lifecycle_engine, activity=activities)
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_TRANSITION_LINEAGE_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)


def test_market_session_without_provider_authority_is_rejected(lifecycle_engine) -> None:
    with (
        pytest.raises(
            DBAPIError,
            match="ALPACA_MARKET_SESSION_PROVIDER_AUTHORITY_INVALID",
        ),
        lifecycle_engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            """
            INSERT INTO alpaca_market_sessions(market_session_id,session_date,open_at,close_at,
              source_hash,session_hash,created_at) VALUES
              ('01000000-0000-0000-0000-000000000099','2026-08-31',
               '2026-08-31 13:30+00','2026-08-31 20:00+00',repeat('4',64),repeat('5',64),
               clock_timestamp())
            """
        )


def test_0014_provider_manifest_commits_and_source_authority_is_append_only(
    lifecycle_engine,
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    template = lifecycle_context()
    retained = replace(
        template,
        thesis_version_id=UUID("10000000-0000-0000-0000-000000000001"),
        account_fingerprint="a" * 64,
        policy_hash="b" * 64,
        position_fingerprint=option_position_fingerprint(
            ((ENTRY_LONG, Decimal("1"), 100), (ENTRY_SHORT, Decimal("-1"), 100))
        ),
        managed_position_id=UUID("80000000-0000-0000-0000-000000000001"),
        current_snapshot_id=UUID("a0000000-0000-0000-0000-000000000001"),
        greek_authority=replace(
            template.greek_authority,
            authority_id=UUID("d0000000-0000-0000-0000-000000000001"),
        ),
        launch_authority=replace(template.launch_authority, entry_policy_hash="b" * 64),
    )
    with lifecycle_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lifecycle_launch_authorities(managed_position_id,beta60,"
                "benchmark_symbol,entry_boundary_at,entry_policy_hash,underlying_source_hash,"
                "benchmark_source_hash,completed_bar_source_hash,created_at) VALUES "
                "(:position,:beta,'QQQ',:boundary,:policy,:underlying,:benchmark,:bar,:created)"
            ),
            {
                "position": retained.managed_position_id,
                "beta": retained.launch_authority.beta60,
                "boundary": retained.launch_authority.entry_boundary_at,
                "policy": retained.policy_hash,
                "underlying": retained.launch_authority.underlying_source_hash,
                "benchmark": retained.launch_authority.benchmark_source_hash,
                "bar": retained.launch_authority.completed_bar_source_hash,
                "created": retained.thesis_frozen_at,
            },
        )
    repository = SQLAlchemyLifecycleRepository(
        sessionmaker(lifecycle_engine, expire_on_commit=False)
    )
    research_payload = {"headline": "Issuer guidance remains unchanged"}
    result_hash = hashlib.sha256(
        json.dumps(research_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    repository.persist_research_sources(
        retained,
        (
            LifecycleResearchSource(
                logical_source_id="source-1",
                source_kind="MCP_NEWS",
                request_hash="6" * 64,
                result_hash=result_hash,
                normalized_payload=research_payload,
                observed_at=NOW,
                retrieved_at=NOW,
                source_hash="9" * 64,
            ),
        ),
        NOW,
    )
    observed = observation()
    observed = replace(
        observed,
        options=tuple(
            replace(item, greek_authority_id=retained.greek_authority.authority_id)
            for item in observed.options
        ),
    )
    repository.persist(
        context=retained,
        observation=observed,
        clusters=(cluster(),),
        classifications=(classification(),),
        manifest_id=UUID("e0000000-0000-0000-0000-000000000002"),
        manifest_hash="f" * 64,
        trusted_at=NOW,
    )
    with lifecycle_engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM lifecycle_observation_manifests "
                    "WHERE manifest_id='e0000000-0000-0000-0000-000000000002'"
                )
            ).scalar_one()
            == 1
        )
    with (
        pytest.raises(DBAPIError, match="LIFECYCLE_AUTHORITY_APPEND_ONLY"),
        lifecycle_engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            "UPDATE lifecycle_source_observations SET request_hash=repeat('7',64) "
            "WHERE external_source_id='source-1'"
        )


def test_transition_rejects_assessment_outside_authoritative_session(
    lifecycle_engine,
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql("SET session_replication_role = replica")
        connection.exec_driver_sql(
            "UPDATE assessment_certificates SET created_at='2026-08-31 15:00+00', "
            "expires_at='2026-08-31 16:00+00' "
            "WHERE certificate_id='30000000-0000-0000-0000-000000000001'"
        )
        connection.exec_driver_sql("SET session_replication_role = origin")
    _install_transition_derivation(lifecycle_engine)
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_TRANSITION_PREDECESSOR_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        _insert_roll_transition(connection)


def test_managed_identity_is_immutable_and_legacy_manifest_route_survives(
    lifecycle_engine,
) -> None:
    _seed_transition_authority(
        lifecycle_engine,
        reconciliation_fingerprint="a" * 64,
        thesis_id="10000000-0000-0000-0000-000000000001",
    )
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_IMMUTABLE"),
        lifecycle_engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            "UPDATE managed_lifecycle_positions SET thesis_version_id="
            "'10000000-0000-0000-0000-000000000099' WHERE managed_position_id="
            "'80000000-0000-0000-0000-000000000001'"
        )
    with (
        pytest.raises(DBAPIError, match="MANAGED_POSITION_CURRENT_SNAPSHOT_INVALID"),
        lifecycle_engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            "UPDATE managed_lifecycle_positions SET closed_at='2026-08-30 15:00+00' "
            "WHERE managed_position_id='80000000-0000-0000-0000-000000000001'"
        )
    with lifecycle_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            INSERT INTO lifecycle_observation_manifests(manifest_id,manifest_hash,
              agent_input_snapshot_id,managed_position_id,managed_snapshot_id,reconciliation_id,
              greek_authority_id,sweep_hash,account_manifest,activity_manifest,option_manifest,
              atm_iv_manifest,underlying_manifest,boundary_manifest,research_manifest,observed_at,
              created_at) VALUES ('e0000000-0000-0000-0000-000000000001',repeat('f',64),
              'c0000000-0000-0000-0000-000000000001','80000000-0000-0000-0000-000000000001',
              'a0000000-0000-0000-0000-000000000001','60000000-0000-0000-0000-000000000001',
              'd0000000-0000-0000-0000-000000000001',repeat('0',64),'{}','[]','[]','{}','{}',
              '{}','[]','2026-08-30 15:00+00','2026-08-30 15:00+00');
            """
        )
    with (
        pytest.raises(DBAPIError, match="LIFECYCLE_AUTHORITY_APPEND_ONLY"),
        lifecycle_engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            "UPDATE lifecycle_observation_manifests SET sweep_hash=repeat('1',64) "
            "WHERE manifest_id='e0000000-0000-0000-0000-000000000001'"
        )
