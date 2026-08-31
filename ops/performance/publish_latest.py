#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.performance.repository import SQLAlchemyPerformanceRepository
from backend.app.persistence.runtime import (
    apply_migrations,
    discover_migrations,
    normalize_database_url,
    verify_schema,
)

ROOT = Path(__file__).parents[2]


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    try:
        apply_migrations(engine, discover_migrations(ROOT / "migrations"))
        verify_schema(engine)
        repository = SQLAlchemyPerformanceRepository(sessionmaker(engine, expire_on_commit=False))
        proof = repository.publish_latest_eligible()
        if proof.publication_hash is None:
            raise RuntimeError("publisher did not create a publication hash")
        print(proof.publication_hash)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
