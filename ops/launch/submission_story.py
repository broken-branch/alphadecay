from __future__ import annotations

import argparse
import errno
import os
import secrets
import stat
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.competition_archive.models import canonical_json, canonical_value
from backend.app.persistence.runtime import normalize_database_url, verify_schema
from backend.app.submission_story import (
    SQLAlchemySubmissionStoryRepository,
    SubmissionDecisionStory,
    SubmissionStoryError,
    build_judge_story,
    build_public_preview,
    render_judge_markdown,
)


class SubmissionStoryLaunchError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview the newest eligible SUBMISSION decision story, or write its private "
            "JSON and Markdown bundle"
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "atomically create private 0600 .json and sibling .md judge-story artifacts; "
            "existing files are refused"
        ),
    )
    return parser


def load_latest_story() -> SubmissionDecisionStory:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SubmissionStoryLaunchError("SUBMISSION_STORY_DATABASE_REQUIRED")
    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    try:
        verify_schema(engine)
        repository = SQLAlchemySubmissionStoryRepository(
            sessionmaker(engine, expire_on_commit=False)
        )
        return repository.latest()
    finally:
        engine.dispose()


def main(
    argv: Sequence[str] | None = None,
    *,
    loader: Callable[[], SubmissionDecisionStory] = load_latest_story,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        story = loader()
        if args.output is None:
            print(canonical_json(build_public_preview(story)))
            return 0
        judge_story = build_judge_story(story)
        _write_private_bundle(
            args.output,
            json_text=canonical_json(canonical_value(judge_story)) + "\n",
            markdown_text=render_judge_markdown(judge_story),
        )
        print(
            canonical_json(
                {
                    "artifacts_written": ["JSON", "MARKDOWN"],
                    "mode": "PRIVATE_0600_JUDGE_STORY_BUNDLE",
                }
            )
        )
        return 0
    except Exception as error:
        if isinstance(error, SystemExit):
            raise
        code = (
            error.code
            if isinstance(error, SubmissionStoryError | SubmissionStoryLaunchError)
            else "SUBMISSION_STORY_EXPORT_FAILED"
        )
        parser.error(code)


def _write_private_bundle(path: Path, *, json_text: str, markdown_text: str) -> None:
    if (
        not path.name
        or path.name.casefold().startswith(".env")
        or path.suffix.casefold() != ".json"
    ):
        raise SubmissionStoryLaunchError("SUBMISSION_STORY_OUTPUT_INVALID")
    markdown_path = path.with_suffix(".md")
    if markdown_path.name.casefold().startswith(".env"):
        raise SubmissionStoryLaunchError("SUBMISSION_STORY_OUTPUT_INVALID")
    directory = _open_parent_directory(path)
    temporary_names: list[str] = []
    linked_names: list[str] = []
    try:
        artifacts = ((path.name, json_text), (markdown_path.name, markdown_text))
        for final_name, text in artifacts:
            temporary_name = f".{final_name}.{secrets.token_hex(16)}.tmp"
            temporary_names.append(temporary_name)
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
            try:
                os.fchmod(descriptor, 0o600)
                payload = text.encode("utf-8")
                offset = 0
                while offset < len(payload):
                    offset += os.write(descriptor, payload[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for (final_name, _text), temporary_name in zip(artifacts, temporary_names, strict=True):
            os.link(
                temporary_name,
                final_name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            linked_names.append(final_name)
        for temporary_name in temporary_names:
            os.unlink(temporary_name, dir_fd=directory)
        temporary_names.clear()
        os.fsync(directory)
    except OSError as error:
        for linked_name in linked_names:
            with suppress(FileNotFoundError):
                os.unlink(linked_name, dir_fd=directory)
        if error.errno == errno.EEXIST:
            raise SubmissionStoryLaunchError("SUBMISSION_STORY_OUTPUT_EXISTS") from None
        raise SubmissionStoryLaunchError("SUBMISSION_STORY_OUTPUT_INVALID") from None
    finally:
        for temporary_name in temporary_names:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory)
        os.close(directory)


def _open_parent_directory(path: Path) -> int:
    parts = path.parent.parts
    if ".." in parts:
        raise SubmissionStoryLaunchError("SUBMISSION_STORY_OUTPUT_INVALID")
    if path.is_absolute():
        descriptor = os.open("/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        components = parts[1:]
    else:
        descriptor = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        components = parts
    try:
        for component in components:
            if component in {"", "."}:
                continue
            next_descriptor = os.open(
                component,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError
        return descriptor
    except OSError:
        os.close(descriptor)
        raise SubmissionStoryLaunchError("SUBMISSION_STORY_OUTPUT_INVALID") from None


if __name__ == "__main__":
    raise SystemExit(main())
