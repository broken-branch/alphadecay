from __future__ import annotations

import argparse
import json
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.contracts.v1 import AccountRole
from backend.app.persistence.opportunity_evidence import (
    OpportunityEvidenceError,
    SQLAlchemyOpportunityEvidenceRepository,
)
from backend.app.persistence.runtime import (
    DatabaseConfigurationError,
    normalize_database_url,
    verify_schema,
)
from backend.app.services.opportunity_bootstrap import (
    OpportunityBootstrapError,
    bootstrap_opportunity,
    parse_opportunity_bootstrap,
)

_INPUT_LIMIT = 1024 * 1024
_DATABASE_URL_LIMIT = 4096


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an exact-role opportunity plan and complete account baseline"
    )
    parser.add_argument(
        "--role",
        default=AccountRole.DEVELOPMENT.value,
        choices=(AccountRole.DEVELOPMENT.value, AccountRole.SUBMISSION.value),
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--persist",
        action="store_true",
        help="freeze the exact validated plan and baseline in an existing runtime database",
    )
    parser.add_argument(
        "--database-url-file",
        type=Path,
        help="private file containing the runtime PostgreSQL URL; required only with --persist",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    account_role = AccountRole(args.role)
    if args.persist != (args.database_url_file is not None):
        parser.error("--persist and --database-url-file must be supplied together")

    engine = None
    try:
        input_reader = (
            _read_private_file if account_role is AccountRole.SUBMISSION else _read_regular_file
        )
        payload = json.loads(input_reader(args.input, _INPUT_LIMIT).decode("utf-8"))
        if account_role is AccountRole.DEVELOPMENT and isinstance(payload, dict):
            payload = payload.copy()
            payload.setdefault("submission_baseline_id", None)
        bootstrap = parse_opportunity_bootstrap(payload, account_role=account_role)
        repository = None
        if args.persist:
            database_url = _read_private_file(args.database_url_file, _DATABASE_URL_LIMIT).decode(
                "utf-8"
            )
            if database_url != database_url.strip() or "\n" in database_url or "\r" in database_url:
                raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_DATABASE_URL_INVALID")
            engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
            verify_schema(engine)
            repository = SQLAlchemyOpportunityEvidenceRepository(
                sessionmaker(engine, expire_on_commit=False)
            )
        result = bootstrap_opportunity(
            bootstrap,
            account_role=account_role,
            persist=args.persist,
            repository=repository,
        )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        OSError,
        OpportunityBootstrapError,
        OpportunityEvidenceError,
        DatabaseConfigurationError,
        SQLAlchemyError,
    ) as error:
        code = getattr(error, "code", "OPPORTUNITY_BOOTSTRAP_INPUT_INVALID")
        parser.error(code)
    finally:
        if engine is not None:
            engine.dispose()

    output = result.sanitized_payload()
    output["account_role"] = account_role.value
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


def _read_regular_file(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_INPUT_INVALID")
        result = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > limit:
                raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_INPUT_INVALID")
        return bytes(result)
    finally:
        os.close(descriptor)


def _read_private_file(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PRIVATE_FILE_INVALID")
        result = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > limit:
                raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_PRIVATE_FILE_INVALID")
        return bytes(result)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
