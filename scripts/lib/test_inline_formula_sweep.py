"""Unit tests for inline-formula retagging."""

from __future__ import annotations

import unittest

from lib.inline_formula_sweep import expand_inline_formula_alt, repair_inline_formula_figures
from lib.figure_alt_sweep import find_suspicious_figure_alts
from lib.layout_table_sweep import repair_layout_tables
from lib.pdf_a11y_audit import audit_pdf_bytes


class InlineFormulaSweepTests(unittest.TestCase):
    def test_expand_cl_hco3_antiporter(self) -> None:
        alt = expand_inline_formula_alt("Erythrocyte Cl-/HCO3- Antiporter")
        lowered = alt.lower()
        self.assertIn("divided by", lowered)
        self.assertIn("antiporter", lowered)
        self.assertGreater(len(alt), 80)

    def test_repair_inline_formula_on_sample_pdf(self) -> None:
        sample = "/tmp/4cdddfe5-latest.pdf"
        try:
            with open(sample, "rb") as handle:
                pdf_bytes = handle.read()
        except FileNotFoundError:
            self.skipTest("sample PDF not available")

        pdf_bytes, result = repair_inline_formula_figures(pdf_bytes)
        self.assertEqual(result.converted, 1)

        pdf_bytes, _layout = repair_layout_tables(pdf_bytes)
        audit = audit_pdf_bytes(pdf_bytes)
        self.assertEqual(find_suspicious_figure_alts(pdf_bytes), [])
        self.assertFalse(audit.has_blocking_issues)


if __name__ == "__main__":
    unittest.main()
