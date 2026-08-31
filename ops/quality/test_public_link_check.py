#!/usr/bin/env python3

"""Focused tests for public Markdown link validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from ops.quality.public_link_check import check_links


class PublicLinkCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "docs").mkdir()
        (self.root / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (self.root / "docs/SETUP.md").write_text("# Setup\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_readme(self, text: str) -> None:
        (self.root / "README.md").write_text(text, encoding="utf-8")

    def test_relative_files_pass_and_external_links_are_ignored(self) -> None:
        self.write_readme(
            "[Setup](docs/SETUP.md) [License](LICENSE) [uv](https://docs.astral.sh/uv/)\n"
        )

        self.assertEqual(check_links(self.root, [Path("README.md")]), [])

    def test_missing_target_fails(self) -> None:
        self.write_readme("[Missing](docs/MISSING.md)\n")

        problems = check_links(self.root, [Path("README.md")])

        self.assertTrue(any("target is missing" in problem for problem in problems))

    def test_parent_escape_and_absolute_path_fail(self) -> None:
        self.write_readme("[Escape](../private.md) [Absolute](/etc/passwd)\n")

        problems = check_links(self.root, [Path("README.md")])

        self.assertTrue(any("leaves the public tree" in problem for problem in problems))
        self.assertTrue(any("not relative" in problem for problem in problems))

    def test_missing_or_external_source_fails(self) -> None:
        outside = self.root.parent / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        self.addCleanup(outside.unlink)

        problems = check_links(self.root, [Path("missing.md"), outside])

        self.assertTrue(any("missing Markdown source" in problem for problem in problems))
        self.assertTrue(any("source is outside" in problem for problem in problems))

    def test_reference_style_and_html_links_are_checked(self) -> None:
        self.write_readme(
            "Read the [setup guide][setup], [setup], or "
            "<a href=docs/SETUP.md>open it</a>.\n\n"
            "[setup]: docs/SETUP.md\n"
        )

        self.assertEqual(check_links(self.root, [Path("README.md")]), [])

        self.write_readme(
            '[Missing][missing] <a href="docs/ALSO_MISSING.md">Missing too</a>\n\n'
            "[missing]: docs/MISSING.md\n"
        )
        problems = check_links(self.root, [Path("README.md")])

        self.assertEqual(sum("target is missing" in problem for problem in problems), 2)

    def test_undefined_reference_fails(self) -> None:
        self.write_readme("Read [setup][undefined].\n")

        problems = check_links(self.root, [Path("README.md")])

        self.assertTrue(any("reference is undefined" in problem for problem in problems))

    def test_markdown_and_explicit_html_anchors_are_checked(self) -> None:
        (self.root / "docs/SETUP.md").write_text(
            '# Local Setup\n\n## Repeated\n\n## Repeated\n\n<a id="manual-anchor"></a>\n',
            encoding="utf-8",
        )
        self.write_readme(
            "[Heading](docs/SETUP.md#local-setup) "
            "[Duplicate](docs/SETUP.md#repeated-1) "
            "[Explicit](docs/SETUP.md#manual-anchor)\n"
        )

        self.assertEqual(check_links(self.root, [Path("README.md")]), [])

        self.write_readme("[Missing anchor](docs/SETUP.md#not-there)\n")
        problems = check_links(self.root, [Path("README.md")])

        self.assertTrue(any("anchor is missing" in problem for problem in problems))

    def test_same_document_anchor_is_checked(self) -> None:
        self.write_readme("# Details\n\n[Jump](#details)\n")

        self.assertEqual(check_links(self.root, [Path("README.md")]), [])

    def test_balanced_markdown_targets_handle_parentheses_escapes_queries_and_decoys(self) -> None:
        (self.root / "docs/SETUP(v2).md").write_text(
            "# Deep Setup\n",
            encoding="utf-8",
        )
        (self.root / "docs/MISSING").write_text("decoy\n", encoding="utf-8")
        self.write_readme(
            "[Nested](docs/SETUP(v2).md?view=full#deep-setup) "
            r"[Escaped](docs/SETUP\(v2\).md#deep-setup) "
            "[Missing](docs/MISSING(v2).md?view=full#deep-setup)\n"
        )

        problems = check_links(self.root, [Path("README.md")])

        self.assertEqual(sum("target is missing" in problem for problem in problems), 1)
        self.assertFalse(any("anchor is missing" in problem for problem in problems))

    def test_html_resource_attributes_and_srcset_are_checked_without_attribute_decoys(self) -> None:
        (self.root / "docs/poster.png").write_bytes(b"poster")
        self.write_readme(
            '<link href="docs/SETUP.md?raw=1#setup" rel="help">\n'
            '<img src="docs/SETUP.md" data-src="docs/DECOY.md">\n'
            '<video poster="docs/poster.png"></video>\n'
            '<source srcset="docs/SETUP.md 1x, docs/MISSING.md?raw=1 2x">\n'
            '<img srcset="docs/SETUP.md, docs/MISSING-NO-DESCRIPTOR.md">\n'
            '<img srcset="data:image/svg+xml,%3Csvg%3E 1x">\n'
            '`<img src="docs/CODE-DECOY.md">`\n'
            '```html\n<img src="docs/FENCE-DECOY.md">\n```\n'
            '<!-- <img src="docs/COMMENT-DECOY.md"> -->\n'
        )

        problems = check_links(self.root, [Path("README.md")])

        self.assertEqual(sum("target is missing" in problem for problem in problems), 2)

    def test_html_resource_traversal_and_encoded_traversal_fail(self) -> None:
        self.write_readme(
            '<img src="../private.png">\n<video poster="docs/%2e%2e/%2e%2e/private.png"></video>\n'
        )

        problems = check_links(self.root, [Path("README.md")])

        self.assertEqual(sum("leaves the public tree" in problem for problem in problems), 2)

    def test_html_object_data_is_checked(self) -> None:
        self.write_readme('<object data="docs/MISSING.pdf"></object>\n')

        problems = check_links(self.root, [Path("README.md")])

        self.assertTrue(any("target is missing" in problem for problem in problems))

    def test_raw_and_repeatedly_encoded_query_private_traversal_is_rejected(self) -> None:
        self.write_readme(
            "[Raw](docs/SETUP.md?next=../private.md)\n"
            '<object data="docs/SETUP.md?next=%252e%252e%252f.private%252fdata"></object>\n'
        )

        problems = check_links(self.root, [Path("README.md")])

        self.assertEqual(sum("unsafe private path" in problem for problem in problems), 2)

    def test_query_decoding_must_reach_a_stable_bounded_value(self) -> None:
        encoded = "../private/data"
        for _iteration in range(12):
            encoded = encoded.replace("%", "%25").replace(".", "%2e").replace("/", "%2f")
        self.write_readme(f"[Deep](docs/SETUP.md?next={encoded})\n")

        problems = check_links(self.root, [Path("README.md")])

        self.assertTrue(any("unsafe private path" in problem for problem in problems))

    def test_oversized_query_expansion_is_rejected(self) -> None:
        self.write_readme("[Large](docs/SETUP.md?next=" + "%25" * 5000 + ")\n")

        problems = check_links(self.root, [Path("README.md")])

        self.assertTrue(any("unsafe private path" in problem for problem in problems))

    def test_query_pairs_reject_identifier_and_raw_payload_values(self) -> None:
        self.write_readme(
            "[Account](docs/SETUP.md?account_id=11111111-2222-4333-8444-000000000001)\n"
            "[Payload](docs/SETUP.md?raw_payload=%7B%22status%22%3A%22filled%22%7D)\n"
        )

        problems = check_links(self.root, [Path("README.md")])

        self.assertEqual(sum("unsafe private data" in problem for problem in problems), 2)

    def test_external_link_queries_are_inspected_without_resolving_the_host(self) -> None:
        self.write_readme("[Broker](https://example.invalid/proof?broker_id=broker-9081726354)\n")

        problems = check_links(self.root, [Path("README.md")])

        self.assertTrue(any("unsafe private data" in problem for problem in problems))

    def test_repeatedly_encoded_embedded_json_is_inspected_recursively(self) -> None:
        self.write_readme(
            "[Nested](docs/SETUP.md?state=%25257B%252522view%252522%25253A%252522ok%252522"
            "%25252C%252522payload%252522%25253A%252522%25257B%25255C%252522trade_id"
            "%25255C%252522%25253A%25255C%25252211111111-2222-4333-8444-000000000001"
            "%25255C%252522%25257D%252522%25257D)\n"
        )

        problems = check_links(self.root, [Path("README.md")])

        self.assertTrue(any("unsafe private data" in problem for problem in problems))

    def test_embedded_json_keeps_encoded_query_separators_inside_string_values(self) -> None:
        self.write_readme(
            "[Nested](docs/SETUP.md?state=%7B%22note%22%3A%22research%26review%22%2C"
            "%22execution_id%22%3A%2211111111-2222-4333-8444-000000000003%22%7D)\n"
        )

        problems = check_links(self.root, [Path("README.md")])

        self.assertTrue(any("unsafe private data" in problem for problem in problems))

    def test_json_stringify_and_payload_wrappers_are_inspected(self) -> None:
        self.write_readme(
            "[Stringify](docs/SETUP.md?state=JSON.stringify(%7B%22provider_id%22%3A"
            "%22provider-9081726354%22%7D))\n"
            "[Payload](docs/SETUP.md?state=payload%3D%257B%2522fill_id%2522%253A"
            "%252211111111-2222-4333-8444-000000000002%2522%257D)\n"
        )

        problems = check_links(self.root, [Path("README.md")])

        self.assertEqual(sum("unsafe private data" in problem for problem in problems), 2)

    def test_external_fragment_and_userinfo_are_inspected(self) -> None:
        self.write_readme(
            "[Fragment](https://example.invalid/proof#activity_id=11111111-2222-4333-8444-000000000005)\n"
            "[Userinfo](https://synthetic-user:synthetic-password@example.invalid/proof)\n"
        )
        problems = check_links(self.root, [Path("README.md")])
        self.assertEqual(sum("unsafe private data" in problem for problem in problems), 2)

    def test_extended_identifier_vocabulary_is_rejected_in_queries(self) -> None:
        identifiers = ("activity_id", "position_id", "request_id", "snapshot_id", "transaction_id")
        self.write_readme(
            "".join(
                f"[{key}](docs/SETUP.md?{key}=11111111-2222-4333-8444-{index:012d})\n"
                for index, key in enumerate(identifiers)
            )
        )
        problems = check_links(self.root, [Path("README.md")])
        self.assertEqual(sum("unsafe private data" in problem for problem in problems), 5)

    def test_nested_spaced_json_stringify_and_malformed_json_fail_closed(self) -> None:
        self.write_readme(
            "[Nested](docs/SETUP.md?state=JSON.stringify%20(%20JSON.stringify%20(%20"
            "%7B%22request_id%22%3A%2211111111-2222-4333-8444-000000000006%22%7D%20)%20))\n"
            "[Malformed](docs/SETUP.md?state=%7B%22provider_id%22%3A)\n"
        )
        problems = check_links(self.root, [Path("README.md")])
        self.assertEqual(sum("unsafe private data" in problem for problem in problems), 2)

    def test_recursive_embedded_payload_depth_is_bounded(self) -> None:
        payload = '{"payload":' * 20 + '"ok"' + "}" * 20

        self.write_readme(f"[Deep](docs/SETUP.md?state={quote(payload)})\n")
        problems = check_links(self.root, [Path("README.md")])
        self.assertTrue(any("unsafe private data" in problem for problem in problems))

    def test_external_paths_and_encoded_fragment_wrappers_are_inspected(self) -> None:
        self.write_readme(
            "[Path](https://example.invalid/request_id/11111111-2222-4333-8444-000000000007)\n"
            "[Encoded](https://example.invalid/proof#%2574ransaction_id%253D11111111-2222-4333-8444-000000000008)\n"
            "[Stringify](https://example.invalid/proof#JSON.stringify%20(%7B%22position_id%22%3A%22"
            "11111111-2222-4333-8444-000000000009%22%7D))\n"
            "[Bracket](https://example.invalid/proof#JSON%5B%22stringify%22%5D(%7B%22activity_id%22"
            "%3A%2211111111-2222-4333-8444-000000000010%22%7D))\n"
        )
        problems = check_links(self.root, [Path("README.md")])
        self.assertEqual(sum("unsafe private data" in problem for problem in problems), 4)

    def test_hash_router_paths_use_the_external_path_identifier_fence(self) -> None:
        self.write_readme(
            "[Route](https://example.invalid/#/request_id/11111111-2222-4333-8444-000000000011)\n"
            "[Encoded](https://example.invalid/#%252Frequest_id%252F"
            "11111111-2222-4333-8444-000000000012)\n"
        )

        problems = check_links(self.root, [Path("README.md")])

        self.assertEqual(sum("unsafe private data" in problem for problem in problems), 2)

    def test_public_hash_router_paths_remain_allowed(self) -> None:
        self.write_readme("[Replay](https://example.invalid/#/replay/THESIS_INTACT)\n")

        self.assertEqual(check_links(self.root, [Path("README.md")]), [])

    def test_malformed_url_returns_redacted_problem(self) -> None:
        self.write_readme("[Malformed](https://[invalid)\n")

        problems = check_links(self.root, [Path("README.md")])

        self.assertTrue(any("malformed URL" in problem for problem in problems))
        self.assertFalse(any("https://[invalid" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
