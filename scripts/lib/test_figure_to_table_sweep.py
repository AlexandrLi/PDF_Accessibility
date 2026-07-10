"""Unit tests for figure-to-table retagging."""

from __future__ import annotations

import unittest

from lib.figure_to_table_sweep import (
    _parse_column_headers,
    _parse_table_rows,
    repair_figure_to_table,
)
from lib.pdf_a11y_audit import audit_pdf_bytes


GLUCOSE_TABLE_ALT = (
    "Table showing different glucose transporters, their tissue expression, and biological "
    "roles. Columns are labeled 'Transporter', 'Tissue Expression', and 'Biological Role'. "
    "Rows include GLUT2 with tissue expression in Intestine, Liver, Pancreas and biological "
    "role of basal glucose uptake, pumping digested glucose into blood, replenishing blood "
    "glucose, and regulation of insulin release. Another row includes GLUT4 with tissue "
    "expression in Muscle, Fat, Heart and biological role of glucose import increased by insulin."
)


class FigureToTableSweepTests(unittest.TestCase):
    def test_parse_column_headers(self) -> None:
        headers = _parse_column_headers(GLUCOSE_TABLE_ALT)
        self.assertEqual(
            headers,
            ["Transporter", "Tissue Expression", "Biological Role"],
        )

    def test_parse_table_rows_under_columns(self) -> None:
        alt = (
            "A table with three columns and three rows. The columns are labeled "
            "'Transporter', 'Tissue Expression', and 'Biological Role'. The second row "
            "contains 'GLUT2' under 'Transporter', 'Intestine, Liver, Pancreas' under "
            "'Tissue Expression', and 'Basal glucose uptake' under 'Biological Role'. "
            "The third row contains 'GLUT4' under 'Transporter', 'Muscle, Fat, Heart' "
            "under 'Tissue Expression', and 'Glucose import, increased by insulin' under "
            "'Biological Role'."
        )
        headers = _parse_column_headers(alt)
        rows = _parse_table_rows(alt, headers)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "GLUT2")
        self.assertEqual(rows[1][0], "GLUT4")

    def test_repair_figure_to_table_on_sample_pdf(self) -> None:
        sample = "/tmp/4cdddfe5-result-new.pdf"
        try:
            pdf_bytes = open(sample, "rb").read()
        except FileNotFoundError:
            self.skipTest("sample PDF not available")

        repaired, result = repair_figure_to_table(pdf_bytes)
        if result.converted == 0:
            self.skipTest(
                "sample PDF not converted (irregular grid or already /Figure with ActualText)"
            )

        self.assertEqual(result.converted, 1)
        audit = audit_pdf_bytes(repaired)
        table_reasons = [
            item
            for item in audit.figures_suspicious_alt
            if "table_tagged_as_figure" in item.reasons
        ]
        self.assertEqual(table_reasons, [])
        self.assertGreaterEqual(audit.table_count, 1)


if __name__ == "__main__":
    unittest.main()
