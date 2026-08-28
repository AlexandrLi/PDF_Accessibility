"""Focused tests for issue-map residual classification."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from sweep_accessibility_issue_map import (  # noqa: E402
    build_residual_diagnostics,
    classify_residual_status,
)


def _sweep_result(
    *,
    audit: dict | None,
    unresolved: list[str] | None = None,
    conflicts: list[str] | None = None,
) -> dict:
    return {
        "repairs": {
            "layoutTable": {
                "unresolved": unresolved or [],
                "conflicts": conflicts or [],
            },
            "auditAfterRepairs": audit,
        }
    }


class AccessibilityIssueMapSweepTests(unittest.TestCase):
    def test_headers_clean_is_resolved(self) -> None:
        diagnostics = build_residual_diagnostics(
            _sweep_result(
                audit={
                    "table_count": 1,
                    "tables_without_th": 0,
                    "data_cells_missing_headers": ["table1 row 2 TD 1"],
                    "tables_th_missing_id": ["table1 TH 1"],
                    "tables_th_duplicate_id": ["table1 TH 2: duplicate"],
                    "invalid_row_child_roles": [],
                    "tables_with_inconsistent_row_widths": [],
                }
            ),
            "Tables Headers",
        )

        self.assertEqual(classify_residual_status(diagnostics), ("resolved", "swept"))

    def test_missing_th_or_explicit_header_reference_is_residual(self) -> None:
        for audit in (
            {"table_count": 1, "tables_without_th": 1},
            {
                "table_count": 1,
                "tables_without_th": 0,
                "data_cells_malformed_headers": ["table1 row 2 TD 1"],
            },
            {
                "table_count": 1,
                "tables_without_th": 0,
                "data_cells_unresolved_headers": ["table1 row 2 TD 1: missing"],
            },
        ):
            with self.subTest(audit=audit):
                diagnostics = build_residual_diagnostics(
                    _sweep_result(audit=audit),
                    "Tables Headers",
                )
                self.assertEqual(
                    classify_residual_status(diagnostics),
                    ("residual", "swept-with-residuals"),
                )

    def test_layout_conflict_is_residual_but_nonblocking(self) -> None:
        diagnostics = build_residual_diagnostics(
            _sweep_result(
                audit={"table_count": 1, "tables_without_th": 0},
                conflicts=["table1: conflicting /Scope"],
            ),
            "Tables Headers",
        )

        self.assertEqual(
            classify_residual_status(diagnostics),
            ("residual", "swept-with-residuals"),
        )
        json.dumps(diagnostics)

    def test_absent_expected_table_is_unverifiable(self) -> None:
        diagnostics = build_residual_diagnostics(
            _sweep_result(audit={"table_count": 0}),
            "Tables Headers; Tables Regularity",
        )

        categories = diagnostics["categories"]
        self.assertEqual(categories["Tables Headers"]["status"], "unverifiable")
        self.assertEqual(categories["Tables Regularity"]["status"], "unverifiable")
        self.assertEqual(
            classify_residual_status(diagnostics),
            ("unverifiable", "swept-unverifiable"),
        )

    def test_regularity_residual_is_explicit(self) -> None:
        diagnostics = build_residual_diagnostics(
            _sweep_result(
                audit={
                    "table_count": 1,
                    "invalid_row_child_roles": ["table1 row 1 child 1"],
                    "tables_with_inconsistent_row_widths": ["table1"],
                    "logical_row_widths": {"table1": [2, 3]},
                }
            ),
            "Tables Regularity",
        )

        self.assertEqual(
            diagnostics["categories"]["Tables Regularity"]["status"],
            "residual",
        )
        self.assertEqual(
            classify_residual_status(diagnostics)[0],
            "residual",
        )


if __name__ == "__main__":
    unittest.main()
