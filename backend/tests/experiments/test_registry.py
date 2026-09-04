from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.experiments.repository import (
    ExperimentRegistryError,
    SQLAlchemyExperimentRegistry,
)
from backend.app.persistence.sqlalchemy_models import (
    Base,
    ExperimentArmEventRow,
    ExperimentArmStateRow,
)
from backend.tests.experiments.fixtures import (
    authorization_request,
    compile_request,
    reviewed_request,
)


def test_registry_creates_and_reads_an_immutable_reviewed_definition() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    registry = SQLAlchemyExperimentRegistry(sessionmaker(engine, expire_on_commit=False))
    now = datetime(2026, 9, 1, 18, tzinfo=UTC)

    created = registry.create(reviewed_request(), created_at=now)
    loaded = registry.read(created.experiment_id)

    assert loaded == created
    assert created.version == 1
    assert created.lifecycle_state == "REVIEWED"
    assert created.automation_state == "OFF"
    assert created.execution_eligible is False
    assert len(created.definition_hash) == 64
    assert created.curation.intake == created.original_thesis
    assert created.curation.protocol_fields == created.reviewed_protocol
    assert registry.list() == (created,)


def test_registry_compiles_once_and_reads_exact_never_armed_version() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    registry = SQLAlchemyExperimentRegistry(sessionmaker(engine, expire_on_commit=False))
    now = datetime(2026, 9, 1, 18, tzinfo=UTC)
    source = registry.create(reviewed_request(), created_at=now)
    request = compile_request(source.definition_hash)

    compiled = registry.compile(source.experiment_id, request, created_at=now)
    repeated = registry.compile(source.experiment_id, request, created_at=now)

    assert repeated == compiled
    assert registry.read_compiled(source.experiment_id) == compiled
    assert compiled.lifecycle_state == "COMPILED"
    assert compiled.arm_state == "NOT_ARMED"
    assert compiled.automation_state == "OFF"
    assert compiled.execution_eligible is False
    assert compiled.protocol_hash == compiled.compiled_protocol.protocol_hash


def test_registry_arms_and_disarms_exact_compiled_protocol_with_audit_revisions() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    registry = SQLAlchemyExperimentRegistry(sessionmaker(engine, expire_on_commit=False))
    now = datetime(2026, 9, 1, 18, tzinfo=UTC)
    source = registry.create(reviewed_request(), created_at=now)
    compiled = registry.compile(
        source.experiment_id,
        compile_request(source.definition_hash),
        created_at=now,
    )
    request = authorization_request(source.definition_hash, compiled.protocol_hash, 0)

    initial = registry.read_authorization(source.experiment_id)
    armed = registry.arm(source.experiment_id, request, changed_at=now)
    repeated = registry.arm(source.experiment_id, request, changed_at=now)
    disarmed = registry.disarm(
        source.experiment_id,
        authorization_request(source.definition_hash, compiled.protocol_hash, 1),
        changed_at=datetime(2026, 9, 1, 18, 1, tzinfo=UTC),
    )

    assert initial.authorization_state == "NOT_ARMED"
    assert initial.authorization_revision == 0
    assert repeated == armed
    assert armed.authorization_state == "ARMED"
    assert armed.entry_authorized is True
    assert armed.runtime_state == "NOT_CONNECTED"
    assert armed.execution_eligible is False
    assert disarmed.authorization_state == "DISARMED"
    assert disarmed.authorization_revision == 2
    assert disarmed.entry_authorized is False
    assert disarmed.existing_position_risk_management_preserved is True
    assert registry.read_authorization(source.experiment_id) == disarmed


def test_registry_allows_only_one_armed_experiment_and_requires_revision() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    registry = SQLAlchemyExperimentRegistry(sessionmaker(engine, expire_on_commit=False))
    now = datetime(2026, 9, 1, 18, tzinfo=UTC)

    def compiled_source():
        source = registry.create(reviewed_request(), created_at=now)
        compiled = registry.compile(
            source.experiment_id,
            compile_request(source.definition_hash),
            created_at=now,
        )
        return source, compiled

    first_source, first_compiled = compiled_source()
    second_source, second_compiled = compiled_source()
    registry.arm(
        first_source.experiment_id,
        authorization_request(first_source.definition_hash, first_compiled.protocol_hash, 0),
        changed_at=now,
    )

    with pytest.raises(ExperimentRegistryError, match="EXPERIMENT_ARM_CONFLICT"):
        registry.arm(
            second_source.experiment_id,
            authorization_request(
                second_source.definition_hash,
                second_compiled.protocol_hash,
                0,
            ),
            changed_at=now,
        )
    with pytest.raises(
        ExperimentRegistryError,
        match="EXPERIMENT_AUTHORIZATION_REVISION_CONFLICT",
    ):
        registry.disarm(
            first_source.experiment_id,
            authorization_request(
                first_source.definition_hash,
                first_compiled.protocol_hash,
                0,
            ),
            changed_at=now,
        )


def test_authorization_rejects_nonmonotonic_time_and_unrelated_idempotency_revision() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    registry = SQLAlchemyExperimentRegistry(sessionmaker(engine, expire_on_commit=False))
    now = datetime(2026, 9, 1, 18, tzinfo=UTC)
    source = registry.create(reviewed_request(), created_at=now)
    compiled = registry.compile(
        source.experiment_id,
        compile_request(source.definition_hash),
        created_at=now,
    )
    registry.arm(
        source.experiment_id,
        authorization_request(source.definition_hash, compiled.protocol_hash, 0),
        changed_at=now,
    )

    with pytest.raises(
        ExperimentRegistryError,
        match="EXPERIMENT_AUTHORIZATION_REVISION_CONFLICT",
    ):
        registry.arm(
            source.experiment_id,
            authorization_request(source.definition_hash, compiled.protocol_hash, 9),
            changed_at=now,
        )
    with pytest.raises(
        ExperimentRegistryError,
        match="EXPERIMENT_AUTHORIZATION_REVISION_CONFLICT",
    ):
        registry.disarm(
            source.experiment_id,
            authorization_request(source.definition_hash, compiled.protocol_hash, 1),
            changed_at=datetime(2026, 9, 1, 17, 59, tzinfo=UTC),
        )

    status = registry.read_authorization(source.experiment_id)
    assert status.authorization_state == "ARMED"
    assert status.authorization_revision == 1
    with engine.begin() as connection:
        connection.execute(
            update(ExperimentArmStateRow)
            .where(ExperimentArmStateRow.experiment_id == source.experiment_id)
            .values(source_definition_hash="f" * 64)
        )
    with pytest.raises(ExperimentRegistryError, match="EXPERIMENT_REGISTRY_STATE_INVALID"):
        registry.read_authorization(source.experiment_id)


def test_schema_binds_authorization_hashes_and_current_state_to_exact_event() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    registry = SQLAlchemyExperimentRegistry(sessionmaker(engine, expire_on_commit=False))
    now = datetime(2026, 9, 1, 18, tzinfo=UTC)

    def compiled_source():
        source = registry.create(reviewed_request(), created_at=now)
        compiled = registry.compile(
            source.experiment_id,
            compile_request(source.definition_hash),
            created_at=now,
        )
        return source, compiled

    first_source, first_compiled = compiled_source()
    second_source, second_compiled = compiled_source()
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert(ExperimentArmEventRow).values(
                event_id=uuid4(),
                experiment_id=first_source.experiment_id,
                source_definition_hash=second_source.definition_hash,
                protocol_hash=second_compiled.protocol_hash,
                authorization_revision=1,
                action="ARM",
                authorization_state="ARMED",
                entry_authorized=True,
                existing_position_risk_management_preserved=True,
                runtime_state="NOT_CONNECTED",
                execution_eligible=False,
                paper_trading_only=True,
                event_hash="f" * 64,
                created_at=now,
            )
        )

    registry.arm(
        first_source.experiment_id,
        authorization_request(first_source.definition_hash, first_compiled.protocol_hash, 0),
        changed_at=now,
    )
    registry.disarm(
        first_source.experiment_id,
        authorization_request(first_source.definition_hash, first_compiled.protocol_hash, 1),
        changed_at=datetime(2026, 9, 1, 18, 1, tzinfo=UTC),
    )
    registry.arm(
        second_source.experiment_id,
        authorization_request(second_source.definition_hash, second_compiled.protocol_hash, 0),
        changed_at=datetime(2026, 9, 1, 18, 2, tzinfo=UTC),
    )
    with engine.begin() as connection:
        second_event_hash = connection.scalar(
            select(ExperimentArmStateRow.last_event_hash).where(
                ExperimentArmStateRow.experiment_id == second_source.experiment_id
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            update(ExperimentArmStateRow)
            .where(ExperimentArmStateRow.experiment_id == first_source.experiment_id)
            .values(last_event_hash=second_event_hash)
        )
