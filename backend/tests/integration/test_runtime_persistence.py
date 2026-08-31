from pathlib import Path

import pytest

from backend.app.persistence.runtime import (
    DatabaseConfigurationError,
    discover_migrations,
    normalize_database_url,
)


def test_render_postgres_url_uses_the_pinned_driver() -> None:
    assert (
        normalize_database_url("postgresql://user:secret@db.example/alphadecay")
        == "postgresql+pg8000://user:secret@db.example/alphadecay"
    )
    assert (
        normalize_database_url("postgres://user:secret@db.example/alphadecay")
        == "postgresql+pg8000://user:secret@db.example/alphadecay"
    )


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///alphadecay.db",
        "mysql://user:secret@db.example/alphadecay",
        "http://db.example/alphadecay",
        "",
    ],
)
def test_runtime_database_must_be_postgres(url: str) -> None:
    with pytest.raises(DatabaseConfigurationError, match="PostgreSQL"):
        normalize_database_url(url)


def test_migrations_are_ordered_and_content_addressed(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text("SELECT 2;\n")
    (tmp_path / "0001_first.sql").write_text("SELECT 1;\n")
    (tmp_path / "README.md").write_text("ignored")

    migrations = discover_migrations(tmp_path)

    assert [migration.version for migration in migrations] == [1, 2]
    assert [migration.filename for migration in migrations] == [
        "0001_first.sql",
        "0002_second.sql",
    ]
    assert all(len(migration.sha256) == 64 for migration in migrations)


def test_duplicate_migration_versions_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1;\n")
    (tmp_path / "0001_again.sql").write_text("SELECT 2;\n")

    with pytest.raises(DatabaseConfigurationError, match="duplicate migration version"):
        discover_migrations(tmp_path)


def test_migration_versions_must_be_contiguous(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1;\n")
    (tmp_path / "0003_third.sql").write_text("SELECT 3;\n")

    with pytest.raises(DatabaseConfigurationError, match="contiguous"):
        discover_migrations(tmp_path)
