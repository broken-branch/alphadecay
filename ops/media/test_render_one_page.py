from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ops.media.render_one_page import (
    ARCHITECTURE_MAX_HEIGHT_IN,
    BODY_FONT_SIZE_PT,
    BODY_LINE_HEIGHT,
    COLUMN_GAP_IN,
    PAGE_HEIGHT_IN,
    PAGE_MARGIN_IN,
    PAGE_WIDTH_IN,
    parse_source,
    render_html,
)

ROOT = Path(__file__).parents[2]
SOURCE = """# alphadecay

Plain tagline.

`PAPER / [[VALUE]]`

> Draft instruction that must not be published.

## First section

One plain paragraph with `NO_ACTION`.

## Second section

Text with <unsafe> markup.
"""


def test_render_uses_source_copy_and_omits_draft_notes() -> None:
    rendered = render_html(SOURCE)

    assert "Plain tagline." in rendered
    assert "<code>PAPER / [[VALUE]]</code>" in rendered
    assert "<code>NO_ACTION</code>" in rendered
    assert "Draft instruction" not in rendered
    assert "&lt;unsafe&gt;" in rendered


def test_parse_rejects_missing_sections() -> None:
    with pytest.raises(ValueError, match="level-two sections"):
        parse_source("# alphadecay\n\nPlain tagline.\n")


def test_parse_does_not_hide_section_block_quotes() -> None:
    source = SOURCE.replace("Text with <unsafe> markup.", "> Public quotation.")

    with pytest.raises(ValueError, match="draft header"):
        parse_source(source)


def test_final_render_rejects_unresolved_markers() -> None:
    with pytest.raises(ValueError, match="unresolved placeholders"):
        render_html(
            SOURCE,
            final=True,
            architecture_asset="assets/architecture.svg",
            reviewed_word_range=(1, 100),
        )

    with pytest.raises(ValueError, match="unresolved placeholders"):
        render_html(
            SOURCE.replace("[[VALUE]]", "TBD_AFTER_DEPLOYMENT"),
            final=True,
            architecture_asset="assets/architecture.svg",
            reviewed_word_range=(1, 100),
        )


def test_final_render_enforces_default_or_explicit_reviewed_word_bound() -> None:
    resolved = SOURCE.replace("[[VALUE]]", "$0 / NO_TRADE")

    with pytest.raises(ValueError, match="650 through 750"):
        render_html(resolved, final=True, architecture_asset="assets/architecture.svg")

    rendered = render_html(
        resolved,
        final=True,
        architecture_asset="assets/architecture.svg",
        reviewed_word_range=(1, 100),
    )
    assert 'name="alphadecay-page-count" content="1"' in rendered
    assert 'name="alphadecay-page-size" content="Letter"' in rendered
    assert 'data-word-count="' in rendered


def test_reviewed_sponsor_source_fits_the_default_final_word_range() -> None:
    source = (ROOT / "submission/one-page-sponsor-writeup.md").read_text(encoding="utf-8")

    rendered = render_html(source, final=True, architecture_asset="architecture.svg")

    assert 'name="alphadecay-page-count" content="1"' in rendered
    assert 'name="alphadecay-page-size" content="Letter"' in rendered
    assert 'data-word-count="744"' in rendered


def test_letter_layout_retains_readable_type_and_balanced_columns() -> None:
    usable_width = PAGE_WIDTH_IN - (2 * PAGE_MARGIN_IN)
    column_width = (usable_width - COLUMN_GAP_IN) / 2

    assert (PAGE_WIDTH_IN, PAGE_HEIGHT_IN) == (8.5, 11.0)
    assert 0.35 <= PAGE_MARGIN_IN <= 0.5
    assert BODY_FONT_SIZE_PT >= 9.0
    assert 1.2 <= BODY_LINE_HEIGHT <= 1.35
    assert 0.3 <= COLUMN_GAP_IN <= 0.5
    assert column_width >= 3.7
    assert 1.4 <= ARCHITECTURE_MAX_HEIGHT_IN <= 1.8

    source = (ROOT / "submission/one-page-sponsor-writeup.md").read_text(encoding="utf-8")
    rendered = render_html(source, final=True, architecture_asset="architecture.svg")

    assert f"@page {{ size: Letter; margin: {PAGE_MARGIN_IN}in; }}" in rendered
    assert (
        f"font-size: {BODY_FONT_SIZE_PT}pt; line-height: {BODY_LINE_HEIGHT};" in rendered
    )
    assert f"columns: 2; column-gap: {COLUMN_GAP_IN}in;" in rendered
    assert f"max-height: {ARCHITECTURE_MAX_HEIGHT_IN}in;" in rendered


@pytest.mark.parametrize(
    "path",
    (
        "/architecture.svg",
        "../architecture.svg",
        "assets\\architecture.svg",
        "https://example.com/architecture.svg",
        "assets/architecture.png",
    ),
)
def test_final_architecture_asset_must_be_a_safe_relative_svg(path: str) -> None:
    resolved = SOURCE.replace("[[VALUE]]", "$0 / NO_TRADE")

    with pytest.raises(ValueError, match="safe relative SVG"):
        render_html(
            resolved,
            final=True,
            architecture_asset=path,
            reviewed_word_range=(1, 100),
        )


def test_final_render_requires_and_embeds_architecture_diagram() -> None:
    resolved = SOURCE.replace("[[VALUE]]", "$0 / NO_TRADE")

    with pytest.raises(ValueError, match="architecture diagram"):
        render_html(resolved, final=True, reviewed_word_range=(1, 100))

    rendered = render_html(
        resolved,
        final=True,
        architecture_asset="assets/architecture.svg",
        reviewed_word_range=(1, 100),
    )
    assert '<img src="assets/architecture.svg" ' in rendered
    assert 'alt="Alpaca MCP research flows through bounded model classification' in rendered
    assert rendered.index('<figure class="architecture">') < rendered.index("<main>")


def test_inline_links_are_clickable_and_restricted_to_reviewed_destinations() -> None:
    source = SOURCE.replace(
        "Text with <unsafe> markup.",
        "Open the [Replay](https://alphadecay.onrender.com).",
    )
    rendered = render_html(source)

    assert '<a href="https://alphadecay.onrender.com">Replay</a>' in rendered

    unsafe = source.replace(
        "https://alphadecay.onrender.com",
        "https://example.com/unreviewed",
    )
    with pytest.raises(ValueError, match="approved public destination"):
        render_html(unsafe)


def test_final_cli_requires_existing_output_relative_architecture(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text(SOURCE.replace("[[VALUE]]", "$0 / NO_TRADE"), encoding="utf-8")
    output = tmp_path / "final/one-page.html"
    output.parent.mkdir()
    command = (
        sys.executable,
        str(Path(__file__).with_name("render_one_page.py")),
        str(source),
        str(output),
        "--final",
        "--architecture-asset",
        "architecture.svg",
        "--reviewed-min-words",
        "1",
        "--reviewed-max-words",
        "100",
    )

    missing = subprocess.run(command, capture_output=True, text=True)
    assert missing.returncode != 0
    assert "architecture diagram does not exist" in missing.stderr

    (output.parent / "architecture.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"/>\n',
        encoding="utf-8",
    )
    subprocess.run(command, check=True)
    assert output.is_file()
