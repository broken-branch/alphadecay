#!/usr/bin/env python3

"""Check that local Markdown links stay within and resolve from a public tree."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit

REFERENCE_LINK = re.compile(r"!?\[([^\]\n]+)\]\[([^\]\n]*)\]")
SHORTCUT_REFERENCE = re.compile(r"(?<!\])!?\[([^\]\n]+)\](?![\[(:])")
REFERENCE_DEFINITION = re.compile(r"(?m)^ {0,3}\[([^\]\n]+)\]:\s*(\S+)")
MARKDOWN_HEADING = re.compile(r"(?m)^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$")
HTML_ANCHOR = re.compile(
    r"(?is)\b(?:id|name)\s*=\s*"
    r'(?:"([^"\r\n]*)"|\'([^\'\r\n]*)\'|([^\s>]+))'
)
INLINE_MARKUP = re.compile(r"!?\[([^\]]+)\]\([^)]+\)|<[^>]+>|[`*_~]")
EXTERNAL_SCHEMES = {"data", "http", "https", "mailto"}
RESOURCE_ATTRIBUTES = {"href", "poster", "src", "srcset"}
MAX_QUERY_LENGTH = 4096
MAX_QUERY_DECODE_PASSES = 8
MAX_QUERY_NODES = 256
MARKDOWN_ESCAPABLE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")
IDENTIFIER_SUFFIXES = (
    "accountid",
    "accountnumber",
    "accountuuid",
    "activityid",
    "brokerid",
    "executionid",
    "fillid",
    "orderid",
    "ordernumber",
    "orderuuid",
    "positionid",
    "providerid",
    "requestid",
    "snapshotid",
    "tradeid",
    "transactionid",
)
IDENTIFIER_ALIASES = (
    "account",
    "activity",
    "broker",
    "execution",
    "fill",
    "order",
    "position",
    "provider",
    "request",
    "snapshot",
    "trade",
    "transaction",
)
RAW_PAYLOAD_KEYS = frozenset(
    {
        "brokerpayload",
        "brokerresponse",
        "executionpayload",
        "providerpayload",
        "providerresponse",
        "rawbrokerpayload",
        "rawpayload",
        "rawproviderpayload",
        "tradepayload",
    }
)
IDENTIFIER_SHAPED = re.compile(
    r"(?i)^(?:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|"
    r"[0-9]{8,}|(?=[a-z0-9_-]{12,}$)(?=[a-z0-9_-]*[0-9])(?=[a-z0-9_-]*[-_])"
    r"[a-z0-9][a-z0-9_-]+)$"
)
MAX_EMBEDDED_DEPTH = 16
CONFUSABLE_CYRILLIC = str.maketrans(
    {"а": "a", "е": "e", "і": "i", "о": "o", "р": "p", "с": "c", "ѕ": "s"}
)


class _HTMLResourceParser(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[int, str]] = []
        self._line_offsets: list[int] = [0]
        self._line_offsets.extend(match.end() for match in re.finditer("\n", text))

    def _record(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        line, column = self.getpos()
        offset = self._line_offsets[line - 1] + column
        for name, value in attributes:
            attribute = name.casefold()
            resource_attribute = attribute in RESOURCE_ATTRIBUTES or (
                tag.casefold() == "object" and attribute == "data"
            )
            if value is None or not resource_attribute:
                continue
            if attribute == "srcset":
                self.links.extend((offset, target) for target in _srcset_targets(value))
            else:
                self.links.append((offset, value))

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._record(tag, attrs)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._record(tag, attrs)


def _srcset_targets(value: str) -> list[str]:
    targets: list[str] = []
    offset = 0
    while offset < len(value):
        while offset < len(value) and (value[offset].isspace() or value[offset] == ","):
            offset += 1
        start = offset
        while offset < len(value) and not value[offset].isspace():
            offset += 1
        raw_target = value[start:offset]
        target = raw_target.rstrip(",")
        if target:
            targets.append(target)
        if raw_target.endswith(","):
            continue
        while offset < len(value) and value[offset] != ",":
            offset += 1
        if offset < len(value):
            offset += 1
    return targets


def _skip_code_span(text: str, offset: int) -> int:
    width = 1
    while offset + width < len(text) and text[offset + width] == "`":
        width += 1
    closing = text.find("`" * width, offset + width)
    return len(text) if closing < 0 else closing + width


def _balanced_markdown_links(text: str) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    offset = 0
    while offset < len(text):
        if text.startswith("<!--", offset):
            closing = text.find("-->", offset + 4)
            offset = len(text) if closing < 0 else closing + 3
            continue
        if text[offset] == "`":
            offset = _skip_code_span(text, offset)
            continue
        if text[offset] == "\\":
            offset += 2
            continue
        image_offset = offset
        if text.startswith("![", offset):
            label_start = offset + 1
        elif text[offset] == "[":
            label_start = offset
        else:
            offset += 1
            continue
        cursor = label_start + 1
        label_depth = 1
        while cursor < len(text) and label_depth:
            if text[cursor] == "\\":
                cursor += 2
                continue
            if text[cursor] == "[":
                label_depth += 1
            elif text[cursor] == "]":
                label_depth -= 1
            cursor += 1
        if label_depth or cursor >= len(text) or text[cursor] != "(":
            offset = label_start + 1
            continue
        target_start = cursor + 1
        cursor = target_start
        parenthesis_depth = 1
        while cursor < len(text) and parenthesis_depth:
            if text[cursor] == "\\":
                cursor += 2
                continue
            if text[cursor] == "\n":
                break
            if text[cursor] == "(":
                parenthesis_depth += 1
            elif text[cursor] == ")":
                parenthesis_depth -= 1
            cursor += 1
        if parenthesis_depth == 0:
            links.append((image_offset, text[target_start : cursor - 1]))
            offset = cursor
        else:
            offset = label_start + 1
    return links


def _mask_markdown_code(text: str) -> str:
    masked = list(text)
    offset = 0
    while offset < len(text):
        if text[offset] == "\\":
            offset += 2
            continue
        if text[offset] != "`":
            offset += 1
            continue
        end = _skip_code_span(text, offset)
        for index in range(offset, end):
            if masked[index] != "\n":
                masked[index] = " "
        offset = end
    return "".join(masked)


def _html_resource_links(text: str) -> list[tuple[int, str]]:
    masked = _mask_markdown_code(text)
    parser = _HTMLResourceParser(masked)
    parser.feed(masked)
    parser.close()
    return parser.links


def _link_parts(raw_target: str) -> tuple[str, str, str, bool, bool] | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        escaped = False
        depth = 0
        destination_end = len(target)
        for index, character in enumerate(target):
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
            elif character == "(":
                depth += 1
            elif character == ")" and depth:
                depth -= 1
            elif character.isspace() and depth == 0:
                destination_end = index
                break
        target = target[:destination_end]
    target = html.unescape(MARKDOWN_ESCAPABLE.sub(r"\1", target))
    parsed = urlsplit(target)
    if parsed.scheme.casefold() in EXTERNAL_SCHEMES or parsed.netloc:
        return (
            parsed.path,
            parsed.fragment,
            parsed.query,
            True,
            bool(parsed.username or parsed.password),
        )
    return unquote(parsed.path), unquote(parsed.fragment), parsed.query, False, False


def _unsafe_query_path(query: str) -> bool:
    if len(query) > MAX_QUERY_LENGTH:
        return True
    decoded = query
    for _iteration in range(MAX_QUERY_DECODE_PASSES):
        expanded = unquote(html.unescape(decoded))
        if len(expanded) > MAX_QUERY_LENGTH:
            return True
        if expanded == decoded:
            break
        decoded = expanded
    else:
        return True
    for component in re.split(r"[&=]", decoded):
        normalized = component.replace("\\", "/").casefold()
        segments = normalized.split("/")
        if normalized.startswith("/") or ".." in segments:
            return True
        if any(segment in {".env", ".git", ".private", "private"} for segment in segments):
            return True
    return False


def _normalized_query_key(value: str) -> str:
    compatible = unicodedata.normalize("NFKC", value).casefold()
    has_latin = any("LATIN" in unicodedata.name(character, "") for character in compatible)
    has_cyrillic = any("CYRILLIC" in unicodedata.name(character, "") for character in compatible)
    if has_latin and has_cyrillic:
        compatible = compatible.translate(CONFUSABLE_CYRILLIC)
    return "".join(character for character in compatible if character.isalnum())


def _stable_query_decode(value: str) -> str | None:
    if len(value) > MAX_QUERY_LENGTH:
        return None
    decoded = value
    for _iteration in range(MAX_QUERY_DECODE_PASSES):
        expanded = unquote(html.unescape(decoded))
        if len(expanded) > MAX_QUERY_LENGTH:
            return None
        if expanded == decoded:
            return decoded
        decoded = expanded
    return None


def _unsafe_query_data(query: str) -> bool:
    if _stable_query_decode(query) is None:
        return True
    remaining = MAX_QUERY_NODES

    def sensitive_pair(key: str, value: object) -> bool:
        decoded_key = _stable_query_decode(key)
        if decoded_key is None:
            return True
        normalized = _normalized_query_key(decoded_key)
        singular = normalized[:-1] if normalized.endswith("s") else normalized
        if normalized in RAW_PAYLOAD_KEYS:
            return value not in (None, "", [], {})
        identifier_key = singular.endswith(IDENTIFIER_SUFFIXES)
        alias_key = singular.endswith(IDENTIFIER_ALIASES)
        if not identifier_key and not alias_key:
            return False
        if identifier_key:
            return value not in (None, "", [], {})
        return isinstance(value, str) and bool(IDENTIFIER_SHAPED.fullmatch(value.strip()))

    def inspect(value: object, key: str = "", depth: int = 0) -> bool:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_EMBEDDED_DEPTH or sensitive_pair(key, value):
            return True
        if isinstance(value, dict):
            return any(
                inspect(child, str(child_key), depth + 1) for child_key, child in value.items()
            )
        if isinstance(value, list):
            return any(inspect(child, key, depth + 1) for child in value)
        if not isinstance(value, str):
            return False
        expanded = _stable_query_decode(value)
        if expanded is None:
            return True
        candidate = expanded.strip()
        stringify = re.fullmatch(
            r"JSON\s*(?:\.\s*stringify|\[\s*[\"']stringify[\"']\s*\])\s*\((.*)\)",
            candidate,
            re.DOTALL,
        )
        if stringify:
            return inspect(stringify.group(1).strip(), key, depth + 1)
        if candidate.casefold().startswith("payload="):
            return inspect(candidate.partition("=")[2].strip(), key, depth + 1)
        if candidate.startswith(("{", "[", '"')):
            try:
                nested = json.loads(candidate)
            except (json.JSONDecodeError, RecursionError):
                return True
            if nested != value and inspect(nested, key, depth + 1):
                return True
        if "=" in candidate:
            try:
                pairs = parse_qsl(
                    candidate,
                    keep_blank_values=True,
                    max_num_fields=MAX_QUERY_NODES,
                )
            except ValueError:
                return True
            if pairs and any(inspect(child, child_key, depth + 1) for child_key, child in pairs):
                return True
        return False

    try:
        pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=MAX_QUERY_NODES)
    except ValueError:
        return True
    return any(inspect(value, key) for key, value in pairs)


def _unsafe_fragment_data(fragment: str) -> bool:
    decoded = _stable_query_decode(fragment)
    if decoded is None:
        return True
    candidate = decoded.strip()
    if candidate.startswith("/"):
        return _unsafe_external_path(candidate)
    if "=" in candidate:
        return _unsafe_query_data(candidate)
    if candidate.startswith(("{", "[", '"')) or re.match(r"JSON\s*(?:\.|\[)", candidate):
        return _unsafe_query_data("value=" + quote(candidate, safe=""))
    return False


def _unsafe_external_path(path: str) -> bool:
    decoded = _stable_query_decode(path)
    if decoded is None:
        return True
    segments = [segment for segment in decoded.split("/") if segment]
    for key, value in zip(segments, segments[1:], strict=False):
        if _unsafe_query_data(f"{quote(key, safe='')}={quote(value, safe='')}"):
            return True
    candidate = decoded.strip("/").strip()
    return (
        bool(candidate)
        and (candidate.startswith(("{", "[", '"')) or re.match(r"JSON\s*(?:\.|\[)", candidate))
        and _unsafe_query_data("value=" + quote(candidate, safe=""))
    )


def _reference_targets(text: str) -> dict[str, str]:
    return {
        label.strip().casefold(): target for label, target in REFERENCE_DEFINITION.findall(text)
    }


def _links(text: str) -> list[tuple[int, str]]:
    targets = _reference_targets(text)
    links = _balanced_markdown_links(text)
    links.extend(_html_resource_links(text))
    for match in REFERENCE_LINK.finditer(text):
        label = (match.group(2) or match.group(1)).strip().casefold()
        if target := targets.get(label):
            links.append((match.start(), target))
        else:
            links.append((match.start(), ""))
    for match in SHORTCUT_REFERENCE.finditer(text):
        if target := targets.get(match.group(1).strip().casefold()):
            links.append((match.start(), target))
    return sorted(links)


def _anchors(text: str) -> set[str]:
    anchors = {
        html.unescape(next(value for value in match.groups() if value is not None))
        for match in HTML_ANCHOR.finditer(text)
    }
    used: dict[str, int] = {}
    for heading in MARKDOWN_HEADING.findall(text):
        plain = html.unescape(INLINE_MARKUP.sub(lambda match: match.group(1) or "", heading))
        slug = re.sub(r"[^\w\- ]", "", plain.casefold()).strip().replace(" ", "-")
        slug = re.sub(r"-+", "-", slug)
        ordinal = used.get(slug, 0)
        used[slug] = ordinal + 1
        anchors.add(slug if ordinal == 0 else f"{slug}-{ordinal}")
    return anchors


def check_links(root: Path, sources: list[Path]) -> list[str]:
    resolved_root = root.resolve(strict=True)
    problems: list[str] = []
    for source in sources:
        source_path = source if source.is_absolute() else resolved_root / source
        try:
            resolved_source = source_path.resolve(strict=True)
        except OSError:
            problems.append(f"missing Markdown source: {source}")
            continue
        if not resolved_source.is_file() or not resolved_source.is_relative_to(resolved_root):
            problems.append(f"Markdown source is outside the public tree: {source}")
            continue
        text = resolved_source.read_text(encoding="utf-8")
        references = _reference_targets(text)
        for match in REFERENCE_LINK.finditer(text):
            label = (match.group(2) or match.group(1)).strip().casefold()
            if label not in references:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{source}:{line}: Markdown reference is undefined")
        for offset, raw_target in _links(text):
            if not raw_target:
                continue
            try:
                parts = _link_parts(raw_target)
            except ValueError:
                line = text.count("\n", 0, offset) + 1
                problems.append(f"{source}:{line}: link contains a malformed URL")
                continue
            if parts is None:
                continue
            link_path, fragment, query, external, userinfo = parts
            line = text.count("\n", 0, offset) + 1
            if _unsafe_query_path(query):
                problems.append(
                    f"{source}:{line}: local link query contains an unsafe private path"
                )
                continue
            if _unsafe_query_data(query):
                problems.append(f"{source}:{line}: local link query contains unsafe private data")
                continue
            if (
                userinfo
                or _unsafe_fragment_data(fragment)
                or external
                and _unsafe_external_path(link_path)
            ):
                problems.append(f"{source}:{line}: link contains unsafe private data")
                continue
            if external:
                continue
            if "\\" in link_path or Path(link_path).is_absolute():
                problems.append(f"{source}:{line}: local link is not relative")
                continue
            candidate = (
                resolved_source
                if not link_path
                else (resolved_source.parent / link_path).resolve(strict=False)
            )
            if not candidate.is_relative_to(resolved_root):
                problems.append(f"{source}:{line}: local link leaves the public tree")
            elif not candidate.exists():
                problems.append(f"{source}:{line}: local link target is missing")
            elif fragment:
                try:
                    target_text = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    problems.append(f"{source}:{line}: local link anchor target is not text")
                else:
                    if fragment not in _anchors(target_text):
                        problems.append(f"{source}:{line}: local link anchor is missing")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("sources", nargs="+", type=Path)
    arguments = parser.parse_args(argv)
    try:
        problems = check_links(arguments.root, arguments.sources)
    except (OSError, UnicodeDecodeError) as error:
        print(f"FAIL  public-link check could not read its inputs: {error}", file=sys.stderr)
        return 1
    if problems:
        print("Public-link check failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print(f"PASS  public-link check ({len(arguments.sources)} Markdown files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
