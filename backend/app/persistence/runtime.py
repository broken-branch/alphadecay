from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from backend.app.order_limits import EntryBudgetLimits

from .agent_repository import AgentDecisionRepository
from .sqlalchemy_models import Base
from .sqlalchemy_repository import (
    SQLAlchemyExecutionRepository,
    SQLAlchemyTrustedDatabaseClock,
)

if TYPE_CHECKING:
    from backend.app.evidence.repository import SQLAlchemyEvidenceLedger
    from backend.app.experiments.performance_reader import SQLAlchemyExperimentPerformanceReader
    from backend.app.experiments.repository import SQLAlchemyExperimentRegistry
    from backend.app.experiments.windows import SQLAlchemyExperimentWindowReader
    from backend.app.lifecycle.repository import SQLAlchemyLifecycleRepository
    from backend.app.performance.repository import SQLAlchemyPerformanceRepository
    from backend.app.persistence.opportunity_authority import (
        SQLAlchemyOpportunityAuthorityRepository,
    )
    from backend.app.persistence.opportunity_evidence import (
        SQLAlchemyOpportunityEvidenceRepository,
    )
    from backend.app.services.opportunity_thesis import (
        SQLAlchemyOpportunityThesisRepository,
    )

MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_[a-z0-9_]+\.sql$")
MIGRATION_LOCK_ID = 6_748_832_219


class DatabaseConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    sql: str
    sha256: str


@dataclass(frozen=True)
class RuntimePersistence:
    engine: Engine
    sessions: sessionmaker
    repository: SQLAlchemyExecutionRepository
    agent_repository: AgentDecisionRepository
    database_clock: RuntimeDatabaseClock
    performance_repository: SQLAlchemyPerformanceRepository
    evidence_ledger: SQLAlchemyEvidenceLedger
    lifecycle_repository: SQLAlchemyLifecycleRepository | None = None
    opportunity_authority_repository: SQLAlchemyOpportunityAuthorityRepository | None = None
    opportunity_evidence_repository: SQLAlchemyOpportunityEvidenceRepository | None = None
    opportunity_thesis_repository: SQLAlchemyOpportunityThesisRepository | None = None
    experiment_registry: SQLAlchemyExperimentRegistry | None = None
    experiment_performance_reader: SQLAlchemyExperimentPerformanceReader | None = None
    experiment_window_reader: SQLAlchemyExperimentWindowReader | None = None

    @property
    def performance_proof_reader(self) -> SQLAlchemyPerformanceRepository:
        return self.performance_repository

    def close(self) -> None:
        self.engine.dispose()


class RuntimeDatabaseClock:
    def __init__(
        self,
        sessions: sessionmaker,
        trusted_clock: SQLAlchemyTrustedDatabaseClock,
    ) -> None:
        self._sessions = sessions
        self._trusted_clock = trusted_clock

    def now(self) -> datetime:
        with self._sessions() as session:
            return self._trusted_clock.now(session)

    def __call__(self) -> datetime:
        return self.now()


def normalize_database_url(url: str) -> str:
    if url.startswith("postgresql+pg8000://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+pg8000://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+pg8000://", 1)
    raise DatabaseConfigurationError("runtime database must use PostgreSQL")


def discover_migrations(directory: Path) -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    versions: set[int] = set()
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            continue
        version = int(match.group("version"))
        if version in versions:
            raise DatabaseConfigurationError(f"duplicate migration version: {version:04d}")
        versions.add(version)
        sql = path.read_text()
        migrations.append(
            Migration(
                version=version,
                filename=path.name,
                sql=sql,
                sha256=hashlib.sha256(sql.encode()).hexdigest(),
            )
        )
    if not migrations:
        raise DatabaseConfigurationError("no database migrations found")
    if [migration.version for migration in migrations] != list(range(1, len(migrations) + 1)):
        raise DatabaseConfigurationError("migration versions must be contiguous from 0001")
    return tuple(migrations)


def create_runtime_persistence(
    database_url: str,
    migrations_directory: Path,
    *,
    entry_limits: EntryBudgetLimits,
    server_autonomy_enabled: bool = False,
) -> RuntimePersistence:
    from backend.app.evidence.repository import SQLAlchemyEvidenceLedger
    from backend.app.experiments.performance_reader import SQLAlchemyExperimentPerformanceReader
    from backend.app.experiments.repository import SQLAlchemyExperimentRegistry
    from backend.app.experiments.windows import SQLAlchemyExperimentWindowReader
    from backend.app.lifecycle.repository import SQLAlchemyLifecycleRepository
    from backend.app.performance.repository import SQLAlchemyPerformanceRepository
    from backend.app.persistence.opportunity_authority import (
        SQLAlchemyOpportunityAuthorityRepository,
    )
    from backend.app.persistence.opportunity_evidence import (
        SQLAlchemyOpportunityEvidenceRepository,
    )
    from backend.app.services.opportunity_thesis import (
        SQLAlchemyOpportunityThesisRepository,
    )

    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    try:
        apply_migrations(engine, discover_migrations(migrations_directory))
        verify_schema(engine)
    except Exception:
        engine.dispose()
        raise
    sessions = sessionmaker(engine, expire_on_commit=False)
    trusted_clock = SQLAlchemyTrustedDatabaseClock()
    database_clock = RuntimeDatabaseClock(sessions, trusted_clock)
    agent_repository = AgentDecisionRepository(
        sessions,
        database_clock=trusted_clock,
        server_autonomy_enabled=server_autonomy_enabled,
    )
    return RuntimePersistence(
        engine=engine,
        sessions=sessions,
        repository=SQLAlchemyExecutionRepository(
            sessions,
            trusted_clock=trusted_clock,
            entry_limits=entry_limits,
        ),
        agent_repository=agent_repository,
        database_clock=database_clock,
        performance_repository=SQLAlchemyPerformanceRepository(sessions),
        evidence_ledger=SQLAlchemyEvidenceLedger(sessions),
        lifecycle_repository=SQLAlchemyLifecycleRepository(sessions),
        opportunity_authority_repository=SQLAlchemyOpportunityAuthorityRepository(sessions),
        opportunity_evidence_repository=SQLAlchemyOpportunityEvidenceRepository(sessions),
        opportunity_thesis_repository=SQLAlchemyOpportunityThesisRepository(sessions),
        experiment_registry=SQLAlchemyExperimentRegistry(sessions),
        experiment_performance_reader=SQLAlchemyExperimentPerformanceReader(sessions),
        experiment_window_reader=SQLAlchemyExperimentWindowReader(sessions),
    )


def apply_migrations(engine: Engine, migrations: tuple[Migration, ...]) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": MIGRATION_LOCK_ID},
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS alphadecay_schema_migrations (
                version integer PRIMARY KEY,
                filename varchar(160) NOT NULL UNIQUE,
                sha256 varchar(64) NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
            """
        )
        applied = {
            row.version: row
            for row in connection.execute(
                text(
                    "SELECT version, filename, sha256 "
                    "FROM alphadecay_schema_migrations ORDER BY version"
                )
            )
        }
        local_versions = {migration.version for migration in migrations}
        unknown = set(applied) - local_versions
        if unknown:
            raise DatabaseConfigurationError("database contains an unknown migration version")
        expected_applied = [migration.version for migration in migrations[: len(applied)]]
        if sorted(applied) != expected_applied:
            raise DatabaseConfigurationError("applied migrations are not a contiguous prefix")
        for migration in migrations:
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded.filename != migration.filename or recorded.sha256 != migration.sha256:
                    raise DatabaseConfigurationError("applied migration checksum does not match")
                continue
            _execute_migration_sql(connection, migration.sql)
            connection.execute(
                text(
                    "INSERT INTO alphadecay_schema_migrations "
                    "(version, filename, sha256) VALUES (:version, :filename, :sha256)"
                ),
                {
                    "version": migration.version,
                    "filename": migration.filename,
                    "sha256": migration.sha256,
                },
            )


def _execute_migration_sql(connection: Connection, sql: str) -> None:
    """Execute trusted migration text with SQLAlchemy-style percent escapes normalized."""
    cursor = connection.connection.driver_connection.cursor()
    try:
        cursor.execute(sql.replace("%%", "%"))
    finally:
        cursor.close()


def verify_schema(engine: Engine) -> None:
    expected = set(Base.metadata.tables)
    with engine.connect() as connection:
        inspector = inspect(connection)
        actual = set(inspector.get_table_names())
        connection.execute(text("SELECT 1"))
        missing = expected - actual
        if missing:
            raise DatabaseConfigurationError("runtime database schema is incomplete")
        for table_name in expected:
            expected_columns = set(Base.metadata.tables[table_name].columns.keys())
            actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
            if expected_columns - actual_columns:
                raise DatabaseConfigurationError("runtime database table is incomplete")
