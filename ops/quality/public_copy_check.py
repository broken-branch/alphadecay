#!/usr/bin/env python3

"""Fail closed on common canned public-copy patterns."""

from __future__ import annotations

import ast
import glob
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_MANIFEST = ROOT / "ops/quality/public-copy-paths.txt"
PUBLIC_RELEASE_MANIFEST = ROOT / "ops/quality/public-release-copy-paths.txt"

BANNED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "canned hype",
        re.compile(
            r"\b(?:revolutionary|groundbreaking|game-changing|cutting-edge|transformative|"
            r"seamless(?:ly)?|supercharge|unleash|elevate|redefine|delve|tapestry)\b",
            re.I,
        ),
    ),
    (
        "canned promise",
        re.compile(
            r"\b(?:unlock(?:ing)?|harness(?:ing)?) the (?:full )?(?:power|potential) of\b",
            re.I,
        ),
    ),
    (
        "generic trend opener",
        re.compile(r"\bin today(?:'|’)s (?:fast-paced|ever-evolving|rapidly evolving)\b", re.I),
    ),
    (
        "generic framing",
        re.compile(r"\b(?:at its core|the future of|stands out from the crowd)\b", re.I),
    ),
    ("formulaic contrast", re.compile(r"\bnot just\b[^.!?\n]{0,100}\bbut also\b", re.I)),
    ("formulaic audience hook", re.compile(r"\bwhether you(?:'|’)re\b", re.I)),
    (
        "assistant meta language",
        re.compile(
            r"\b(?:as an AI|in conclusion|it is important to note|it(?:'|’)s important to note|"
            r"it is worth noting|it(?:'|’)s worth noting|let(?:'|’)s dive)\b",
            re.I,
        ),
    ),
    (
        "inflated product claim",
        re.compile(
            r"\b(?:next[- ]generation|innovative solution|powerful platform|"
            r"robust and scalable)\b",
            re.I,
        ),
    ),
    (
        "formulaic transition",
        re.compile(
            r"\b(?:here(?:'|’)s the thing|here(?:'|’)s why|that said|with that in mind|"
            r"to put it simply)\b",
            re.I,
        ),
    ),
    (
        "formulaic summary",
        re.compile(r"\b(?:the key takeaway|the bottom line is|what this means is)\b", re.I),
    ),
    (
        "empty certainty",
        re.compile(
            r"\b(?:needless to say|without a doubt|undeniably|clearly demonstrates)\b", re.I
        ),
    ),
    (
        "private numeric risk limit",
        re.compile(
            r"\b(?:maximum(?: position)? loss|position loss|risk (?:cap|limit)|quantity|"
            r"contract (?:cap|limit))\b[^.!?\n]{0,100}(?:"
            r"\$\s?\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?\s*%|"
            r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+)"
            r"\s+contracts?\b)",
            re.I,
        ),
    ),
)

FENCE = re.compile(r"```.*?```", re.S)
MARKDOWN_LINK_TARGET = re.compile(r"\]\([^)]*\)")
URL = re.compile(r"https?://\S+")
SENTENCE = re.compile(r"(?<=[.!?])\s+")
WORD = re.compile(r"\b[\w’'-]+\b")
ENV_ASSIGNMENT = re.compile(r"[A-Z][A-Z0-9_]*=.*")
MAX_SENTENCE_WORDS = 38
MAX_PARAGRAPH_WORDS = 110


def manifest_patterns(manifest: Path) -> list[str]:
    patterns: list[str] = []
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            patterns.append(value)
    if not patterns:
        raise ValueError("public-copy manifest is empty")
    return patterns


def registered_files() -> list[Path]:
    manifest = PRIVATE_MANIFEST if PRIVATE_MANIFEST.is_file() else PUBLIC_RELEASE_MANIFEST
    files: set[Path] = set()
    for pattern in manifest_patterns(manifest):
        matches = [Path(value) for value in glob.glob(str(ROOT / pattern), recursive=True)]
        regular = [path for path in matches if path.is_file()]
        if not regular:
            raise ValueError(f"public-copy pattern matched no files: {pattern}")
        files.update(regular)
    return sorted(files)


def prose_for_checks(text: str) -> str:
    text = FENCE.sub("", text)
    text = MARKDOWN_LINK_TARGET.sub("]", text)
    return URL.sub("", text)


def check_text(source_label: str, raw_text: str) -> list[str]:
    text = prose_for_checks(raw_text)
    problems: list[str] = []

    if "—" in text:
        problems.append(f"{source_label}: replace em dash with simpler punctuation")

    for pattern_label, pattern in BANNED_PATTERNS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{source_label}:{line}: {pattern_label}: {match.group(0)!r}")

    prose_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
        and not line.lstrip().startswith(("#", "- ", "* ", ">", "|", "<"))
        and not (source_label.endswith(".env.example") and ENV_ASSIGNMENT.fullmatch(line.strip()))
    ]
    prose = "\n".join(prose_lines)
    for sentence in SENTENCE.split(prose):
        words = WORD.findall(sentence)
        if len(words) > MAX_SENTENCE_WORDS:
            preview = " ".join(words[:10])
            problems.append(
                f"{source_label}: sentence has {len(words)} words "
                f"(max {MAX_SENTENCE_WORDS}): {preview!r}"
            )

    for paragraph in re.split(r"\n\s*\n", text):
        if paragraph.lstrip().startswith(("#", "|", "```")):
            continue
        words = WORD.findall(paragraph)
        if len(words) > MAX_PARAGRAPH_WORDS:
            problems.append(
                f"{source_label}: paragraph has {len(words)} words (max {MAX_PARAGRAPH_WORDS})"
            )

    return problems


def json_string_values(value: object, pointer: str = "$") -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    if isinstance(value, str):
        values.append((pointer, value))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            values.extend(json_string_values(item, f"{pointer}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            values.extend(json_string_values(item, f"{pointer}.{key}"))
    return values


def check_json_text(source_label: str, raw_text: str) -> list[str]:
    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError as error:
        return [f"{source_label}:{error.lineno}: invalid JSON in public-copy catalog: {error.msg}"]

    if not isinstance(value, dict):
        return [f"{source_label}: public-copy JSON must be an object"]

    strings = json_string_values(value)
    if not strings:
        return [f"{source_label}: public-copy JSON contains no string values"]
    blank_values = [
        f"{source_label}:{pointer}: public-copy value is blank"
        for pointer, text in strings
        if not text.strip()
    ]
    return [
        problem
        for pointer, text in strings
        for problem in check_text(f"{source_label}:{pointer}", text)
    ] + blank_values


def check_python_text(source_label: str, raw_text: str) -> list[str]:
    try:
        tree = ast.parse(raw_text)
    except SyntaxError as error:
        return [f"{source_label}:{error.lineno}: invalid Python in public-copy source"]

    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    if not strings:
        return [f"{source_label}: public-copy Python contains no string values"]
    return [
        problem
        for index, value in enumerate(strings)
        for problem in check_text(f"{source_label}:python-copy[{index}]", value)
    ]


def check_svg_text(source_label: str, raw_text: str) -> list[str]:
    try:
        root = ET.fromstring(raw_text)
    except ET.ParseError as error:
        return [f"{source_label}: invalid SVG in public-copy asset: {error}"]

    authored = [
        "".join(element.itertext()).strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] in {"title", "desc"}
    ]
    return [
        problem
        for index, text in enumerate(authored)
        for problem in check_text(f"{source_label}:svg-copy[{index}]", text)
    ]


def check(path: Path) -> list[str]:
    relative = path.relative_to(ROOT)
    text = path.read_text(encoding="utf-8")
    if path.suffix.casefold() == ".json":
        return check_json_text(str(relative), text)
    if path.suffix.casefold() == ".py":
        return check_python_text(str(relative), text)
    if path.suffix.casefold() == ".svg":
        return check_svg_text(str(relative), text)
    return check_text(str(relative), text)


def main() -> int:
    try:
        paths = registered_files()
    except (OSError, ValueError) as error:
        print(f"FAIL  {error}", file=sys.stderr)
        return 1

    problems = [problem for path in paths for problem in check(path)]
    if problems:
        print("Public-copy check failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        print("Rewrite flagged copy; do not suppress the rule.", file=sys.stderr)
        return 1

    print(f"PASS  public-copy check ({len(paths)} registered files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
