#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFAULT_REVIEWED_WORD_RANGE = (650, 750)
EXPECTED_PAGE_COUNT = 1
PAGE_SIZE = "Letter"
PAGE_WIDTH_IN = 8.5
PAGE_HEIGHT_IN = 11.0
PAGE_MARGIN_IN = 0.36
BODY_FONT_SIZE_PT = 9.0
BODY_LINE_HEIGHT = 1.23
COLUMN_GAP_IN = 0.34
ARCHITECTURE_MAX_HEIGHT_IN = 1.6
_UNRESOLVED = re.compile(r"\[\[|\bTBD(?:_[A-Z0-9_]+)?\b|\bPLACEHOLDER\b")
_WORD = re.compile(r"(?<![\w])(?:\$?[\w]+(?:[./'-][\w]+)*%?)(?![\w])")
_INLINE_MARKUP = re.compile(r"`([^`\n]+)`|\[([^\]\n]+)\]\((https://[^\s)]+)\)")
_ALLOWED_LINKS = frozenset(
    {
        "https://alphadecay.onrender.com",
        "https://alphadecay.onrender.com/api/competition-record",
        "https://alphadecay.onrender.com/docs#/Replay/anonymous_replay",
        "https://github.com/broken-branch/alphadecay/actions",
    }
)


@dataclass(frozen=True)
class Section:
    title: str
    paragraphs: tuple[str, ...]


def _paragraphs(lines: list[str], *, allow_draft_note: bool = False) -> tuple[str, ...]:
    paragraphs: list[str] = []
    pending: list[str] = []

    def finish() -> None:
        if pending:
            paragraphs.append(" ".join(pending))
            pending.clear()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            finish()
        elif line.startswith(">"):
            if not allow_draft_note:
                raise ValueError("block quotes are allowed only in the draft header")
            finish()
        else:
            pending.append(line)
    finish()
    return tuple(paragraphs)


def parse_source(source: str) -> tuple[str, tuple[str, ...], tuple[Section, ...]]:
    lines = source.splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError("one-page source must begin with one level-one heading")

    title = lines[0][2:].strip()
    introduction: list[str] = []
    sections: list[Section] = []
    section_title: str | None = None
    section_lines: list[str] = []

    for raw_line in lines[1:]:
        if raw_line.startswith("## "):
            if section_title is not None:
                sections.append(Section(section_title, _paragraphs(section_lines)))
            section_title = raw_line[3:].strip()
            section_lines = []
        elif section_title is None:
            introduction.append(raw_line)
        else:
            section_lines.append(raw_line)

    if section_title is not None:
        sections.append(Section(section_title, _paragraphs(section_lines)))
    if not sections:
        raise ValueError("one-page source must contain level-two sections")

    return title, _paragraphs(introduction, allow_draft_note=True), tuple(sections)


def _inline(text: str) -> str:
    rendered: list[str] = []
    offset = 0
    for match in _INLINE_MARKUP.finditer(text):
        rendered.append(html.escape(text[offset : match.start()], quote=True))
        code, label, url = match.groups()
        if code is not None:
            rendered.append(f"<code>{html.escape(code, quote=True)}</code>")
        else:
            if url not in _ALLOWED_LINKS:
                raise ValueError("one-page link is not an approved public destination")
            rendered.append(
                f'<a href="{html.escape(url, quote=True)}">'
                f"{html.escape(label, quote=True)}</a>"
            )
        offset = match.end()
    rendered.append(html.escape(text[offset:], quote=True))
    return "".join(rendered)


def _title_markup(title: str) -> str:
    if title == "alphadecay":
        return '<span class="brand-alpha">α</span><span>lphadecay</span>'
    return _inline(title)


def _architecture_asset(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or (path.parts and ":" in path.parts[0])
        or path.suffix.lower() != ".svg"
    ):
        raise ValueError("architecture diagram must use a safe relative SVG path")
    return value


def _require_architecture_asset(output: Path, value: str) -> None:
    relative = PurePosixPath(_architecture_asset(value))
    root = output.parent.resolve(strict=True)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("architecture diagram may not traverse symbolic links")
    try:
        resolved = current.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError("architecture diagram does not exist") from None
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("architecture diagram must be a regular output-relative file")


def _word_count(title: str, introduction: tuple[str, ...], sections: tuple[Section, ...]) -> int:
    visible = (title, *introduction) + tuple(
        value for section in sections for value in (section.title, *section.paragraphs)
    )
    return sum(len(_WORD.findall(value)) for value in visible)


def render_html(
    source: str,
    *,
    final: bool = False,
    architecture_asset: str | None = None,
    reviewed_word_range: tuple[int, int] = DEFAULT_REVIEWED_WORD_RANGE,
) -> str:
    if final and _UNRESOLVED.search(source):
        raise ValueError("final one-page copy contains unresolved placeholders")
    title, introduction, sections = parse_source(source)
    if len(introduction) != 2:
        raise ValueError("one-page source must have one tagline and one status line")

    minimum, maximum = reviewed_word_range
    if minimum < 1 or maximum < minimum:
        raise ValueError("reviewed word range must contain positive ordered bounds")
    word_count = _word_count(title, introduction, sections)
    if final and not minimum <= word_count <= maximum:
        raise ValueError(
            f"final one-page copy must contain {minimum} through {maximum} words; "
            f"found {word_count}"
        )
    if final and architecture_asset is None:
        raise ValueError("final one-page render requires an architecture diagram")
    diagram = ""
    if architecture_asset is not None:
        diagram = (
            '<figure class="architecture">'
            f'<img src="{_inline(_architecture_asset(architecture_asset))}" '
            'alt="Alpaca MCP research flows through bounded model classification and fixed '
            'policy before a paper Trading API action; the Alpaca CLI remains outside the '
            'application.">'
            "</figure>"
        )

    rendered_sections = "\n".join(
        "<section>"
        f"<h2>{_inline(section.title)}</h2>"
        + "".join(f"<p>{_inline(paragraph)}</p>" for paragraph in section.paragraphs)
        + "</section>"
        for section in sections
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="alphadecay-page-count" content="{EXPECTED_PAGE_COUNT}">
  <meta name="alphadecay-page-size" content="{PAGE_SIZE}">
  <title>{_inline(title)}</title>
  <style>
    @page {{ size: {PAGE_SIZE}; margin: {PAGE_MARGIN_IN}in; }}
    * {{ box-sizing: border-box; }}
    html {{
      color: #242326; background: #f6f6f6;
      font-family: "IBM Plex Sans", system-ui, sans-serif;
    }}
    body {{ margin: 0; font-size: {BODY_FONT_SIZE_PT}pt; line-height: {BODY_LINE_HEIGHT}; }}
    header {{ padding: 0.01in 0 0.14in; border-bottom: 2px solid #684fc6; }}
    h1 {{
      margin: 0; color: #242326; font-size: 27pt; line-height: 1;
      font-weight: 600; letter-spacing: -0.035em;
    }}
    .brand-alpha {{ color: #684fc6; }}
    .tagline {{ margin: 0.075in 0 0; color: #4f4d52; font-size: 11.5pt; }}
    .status {{
      margin: 0.085in 0 0;
      padding: 0.05in 0.09in;
      color: #3e2f85;
      background: #e7e0ff;
      border: 1px solid #a991ff;
      border-radius: 4px;
      font-size: 8.2pt;
      font-weight: 700;
      letter-spacing: 0.025em;
    }}
    main {{ columns: 2; column-gap: {COLUMN_GAP_IN}in; column-rule: 1px solid #dedbe3; }}
    section {{ margin: 0 0 0.075in; }}
    .architecture {{
      margin: 0.09in 0; padding: 0.055in;
      border: 1px solid #b9b5ae; border-radius: 4px;
    }}
    .architecture img {{
      display: block; width: 100%; max-height: {ARCHITECTURE_MAX_HEIGHT_IN}in;
      object-fit: contain;
    }}
    h2 {{
      break-after: avoid; margin: 0 0 0.035in;
      color: #684fc6; font-size: 10.5pt; line-height: 1.1;
    }}
    p {{ break-inside: avoid; margin: 0 0 0.045in; orphans: 3; widows: 3; }}
    code {{
      color: #4f4d52;
      font-family: "IBM Plex Mono", ui-monospace, monospace;
      font-size: 0.92em;
      font-weight: 700;
    }}
    a {{ color: #3e2f85; font-weight: 600; text-decoration-thickness: 0.06em; }}
  </style>
</head>
<body data-word-count="{word_count}">
  <header>
    <h1>{_title_markup(title)}</h1>
    <p class="tagline">{_inline(introduction[0])}</p>
    <p class="status">{_inline(introduction[1])}</p>
  </header>
  {diagram}
  <main>
    {rendered_sections}
  </main>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--architecture-asset")
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--reviewed-min-words", type=int)
    parser.add_argument("--reviewed-max-words", type=int)
    args = parser.parse_args()

    if (args.reviewed_min_words is None) != (args.reviewed_max_words is None):
        parser.error("reviewed word bounds must be supplied together")
    word_range = DEFAULT_REVIEWED_WORD_RANGE
    if args.reviewed_min_words is not None and args.reviewed_max_words is not None:
        word_range = (args.reviewed_min_words, args.reviewed_max_words)
    if args.final and args.architecture_asset is not None:
        _require_architecture_asset(args.output, args.architecture_asset)
    rendered = render_html(
        args.source.read_text(encoding="utf-8"),
        final=args.final,
        architecture_asset=args.architecture_asset,
        reviewed_word_range=word_range,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
