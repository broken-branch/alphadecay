#!/usr/bin/env python3

"""Focused tests for the public-copy release gate."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_copy_check import check_json_text, check_python_text, check_svg_text, check_text


class PublicCopyCheckTests(unittest.TestCase):
    def test_plain_copy_passes(self) -> None:
        self.assertEqual(
            check_text(
                "sample",
                "alphadecay keeps the original trade note beside the current position. "
                "It shows what changed and why that change matters.",
            ),
            [],
        )

    def test_canned_hype_fails(self) -> None:
        problems = check_text("sample", "A groundbreaking platform will redefine trading.")
        self.assertTrue(any("canned hype" in problem for problem in problems))

    def test_formulaic_transition_fails(self) -> None:
        problems = check_text("sample", "Here's why this trade matters.")
        self.assertTrue(any("formulaic transition" in problem for problem in problems))

    def test_em_dash_fails(self) -> None:
        problems = check_text("sample", "The position changed—and the reason matters.")
        self.assertTrue(any("em dash" in problem for problem in problems))

    def test_exact_private_risk_limit_fails(self) -> None:
        problems = check_text(
            "sample",
            "Maximum loss is the smaller of $1,000 and 1% of equity; "
            "quantity is capped at six contracts.",
        )
        self.assertTrue(any("private numeric risk limit" in problem for problem in problems))

    def test_nonnumeric_risk_gate_copy_passes(self) -> None:
        self.assertEqual(
            check_text(
                "sample",
                "Private server-side limits cap both position loss and quantity before entry.",
            ),
            [],
        )

    def test_long_sentence_fails(self) -> None:
        sentence = " ".join(["word"] * 39) + "."
        problems = check_text("sample", sentence)
        self.assertTrue(any("sentence has 39 words" in problem for problem in problems))

    def test_env_assignments_do_not_form_one_synthetic_sentence(self) -> None:
        assignments = "\n".join(f"SETTING_{index}=placeholder" for index in range(50))

        self.assertEqual(check_text(".env.example", assignments), [])

    def test_json_checks_values_but_not_keys(self) -> None:
        problems = check_json_text(
            "copy.json",
            '{"groundbreaking": "A groundbreaking platform.", "plain": "Check this trade."}',
        )
        self.assertEqual(sum("canned hype" in problem for problem in problems), 1)
        self.assertTrue(any("$.groundbreaking" in problem for problem in problems))

    def test_invalid_json_fails(self) -> None:
        problems = check_json_text("copy.json", '{"title": }')
        self.assertTrue(any("invalid JSON" in problem for problem in problems))

    def test_json_catalog_must_be_an_object(self) -> None:
        problems = check_json_text("copy.json", '["Check this trade."]')
        self.assertTrue(any("must be an object" in problem for problem in problems))

    def test_json_catalog_without_strings_fails(self) -> None:
        problems = check_json_text("copy.json", '{"enabled": true}')
        self.assertTrue(any("contains no string values" in problem for problem in problems))

    def test_python_checks_copy_values_without_treating_identifiers_as_prose(self) -> None:
        source = '\n'.join(
            (
                'LONG_INTERNAL_IDENTIFIER = "Plain route description."',
                'SECOND_LONG_INTERNAL_IDENTIFIER = "Another short description."',
            )
        )

        self.assertEqual(check_python_text("copy.py", source), [])

    def test_python_copy_values_still_use_public_copy_rules(self) -> None:
        problems = check_python_text("copy.py", 'DESCRIPTION = "A groundbreaking platform."')

        self.assertTrue(any("canned hype" in problem for problem in problems))

    def test_invalid_python_fails(self) -> None:
        problems = check_python_text("copy.py", 'DESCRIPTION = "missing close')

        self.assertTrue(any("invalid Python" in problem for problem in problems))

    def test_blank_json_value_fails(self) -> None:
        problems = check_json_text("copy.json", '{"title": "  "}')
        self.assertTrue(any("value is blank" in problem for problem in problems))

    def test_svg_checks_accessible_copy_without_treating_path_data_as_prose(self) -> None:
        path_data = " ".join(f"L {value} {value}" for value in range(150))
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            "<title>alphadecay</title>"
            "<desc>Position review logo.</desc>"
            f'<path d="{path_data}" />'
            "</svg>"
        )

        self.assertEqual(check_svg_text("brand.svg", svg), [])

    def test_svg_accessible_copy_still_uses_public_copy_rules(self) -> None:
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><title>Revolutionary trading</title></svg>'

        self.assertTrue(
            any("canned hype" in problem for problem in check_svg_text("brand.svg", svg))
        )


if __name__ == "__main__":
    unittest.main()
