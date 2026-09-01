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
    normalized_chapter_title,
    resolve_topics,
)


def _course_with_chapter(chapter_title: str, extra_chapters: list | None = None) -> dict:
    chapters = [
        {"id": "ch10", "title": chapter_title, "topics": [{"id": "topic-1"}]}
    ] + (extra_chapters or [])
    return {
        "details": {"title": "Course", "defaultToc": "toc"},
        "tocs": {"toc": {"chapters": chapters}},
        "topics": {
            "topic-1": {"title": "The Quadratic Formula", "pdfAvailable": True},
        },
    }


def _issue_row(chapter_id: str = "10") -> dict:
    return {
        "sheet": "Course",
        "row": "68",
        "topic_title": "The Quadratic Formula",
        "chapter_id": chapter_id,
        "chapter_title": "Quadratic Equations & Applications",
        "chapter_row": "65",
        "failed_columns": "Z",
        "failed_categories": "Tables Headers",
        "failure_count": "1",
        "topic_id": "",
        "pdf_key": "",
    }


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
    def test_chapter_title_matching_treats_ampersand_as_and(self) -> None:
        self.assertEqual(
            normalized_chapter_title("1. Equations and Inequalities"),
            normalized_chapter_title("Equations & Inequalities"),
        )

    def test_unique_title_with_matching_chapter_number_is_accepted(self) -> None:
        course = _course_with_chapter("10. Quadratic Equations")
        matched, unmatched, _ = resolve_topics([_issue_row()], course, "course")
        self.assertEqual(unmatched, [])
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["topicId"], "topic-1")
        self.assertEqual(matched[0]["matchStrategy"], "uniqueTitleChapterNumber")

    def test_unique_title_with_wrong_chapter_number_stays_unmatched(self) -> None:
        course = _course_with_chapter("9. Quadratic Equations")
        matched, unmatched, _ = resolve_topics([_issue_row()], course, "course")
        self.assertEqual(matched, [])
        self.assertEqual(len(unmatched), 1)

    def test_exact_chapter_title_match_keeps_priority(self) -> None:
        course = _course_with_chapter("10. Quadratic Equations and Applications")
        matched, unmatched, _ = resolve_topics([_issue_row()], course, "course")
        self.assertEqual(unmatched, [])
        self.assertEqual(matched[0]["matchStrategy"], "chapterTitle")

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

    def test_untagged_pdf_is_not_classified_as_resolved(self) -> None:
        diagnostics = build_residual_diagnostics(
            _sweep_result(
                audit={
                    "struct_tree_root_present": False,
                    "table_count": 0,
                }
            ),
            "Tab Order",
        )

        self.assertEqual(
            diagnostics["categories"]["Tagged Content"]["status"],
            "residual",
        )
        self.assertEqual(
            classify_residual_status(diagnostics),
            ("residual", "swept-with-residuals"),
        )

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
