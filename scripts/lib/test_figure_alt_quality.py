"""Unit tests for figure alt quality heuristics."""

from __future__ import annotations

import unittest

from figure_alt_quality import classify_figure_alt, is_suspicious_figure_alt


class FigureAltQualityTests(unittest.TestCase):
    def test_truncated_glut1_diagram(self) -> None:
        alt = (
            "Diagram illustrating the process of glucose transport via GLUT1 uniporter "
            "in erythrocytes. It shows four stages: 1"
        )
        reasons = classify_figure_alt(alt)
        self.assertIn("truncated_list_enumeration", reasons)
        self.assertTrue(is_suspicious_figure_alt(alt))

    def test_complete_diagram_alt_passes(self) -> None:
        alt = (
            "Diagram illustrating glucose transport via GLUT1 with four labeled stages "
            "from extracellular glucose binding through release inside the cell."
        )
        self.assertFalse(is_suspicious_figure_alt(alt))

    def test_inline_formula_caption_only(self) -> None:
        alt = "Erythrocyte Cl-/HCO3- Antiporter"
        reasons = classify_figure_alt(
            alt,
            struct_classes={"/fb-region-inlineFormula"},
        )
        self.assertIn("inline_formula_caption_only", reasons)

    def test_table_as_figure(self) -> None:
        alt = "Table showing different types of glucose transporters and tissue expression."
        reasons = classify_figure_alt(alt)
        self.assertIn("table_tagged_as_figure", reasons)

    def test_a_table_with_columns_and_rows(self) -> None:
        alt = (
            "A table with three columns and three rows. The columns are labeled "
            "'Transporter', 'Tissue Expression', and 'Biological Role'. The second row "
            "contains 'GLUT2' under 'Transporter', 'Intestine, Liver, Pancreas' under "
            "'Tissue Expression', and 'Basal glucose uptake' under 'Biological Role'. "
            "The third row contains 'GLUT4' under 'Transporter', 'Muscle, Fat, Heart' "
            "under 'Tissue Expression', and 'Glucose import, increased by insulin' under "
            "'Biological Role'."
        )
        reasons = classify_figure_alt(alt)
        self.assertEqual(reasons, [])

    def test_empty_alt_not_suspicious(self) -> None:
        self.assertFalse(is_suspicious_figure_alt(""))
        self.assertFalse(is_suspicious_figure_alt("   "))


if __name__ == "__main__":
    unittest.main()
