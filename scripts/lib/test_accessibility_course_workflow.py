"""Tests for reusable accessibility PDF preparation."""

from __future__ import annotations

import io
import unittest

import pikepdf

from lib.accessibility_course_workflow import prepare_pdf, run_sweeps


def _blank_pdf(*, tabs: bool) -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    if tabs:
        page.obj["/Tabs"] = pikepdf.Name("/S")
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


class AccessibilityCourseWorkflowTests(unittest.TestCase):
    def test_unchanged_first_pass_skips_second_full_sweep(self) -> None:
        original = _blank_pdf(tabs=True)

        repaired, result = prepare_pdf(original)

        self.assertEqual(repaired, original)
        self.assertFalse(result["secondPass"]["executed"])
        self.assertTrue(result["secondPass"]["byteStable"])
        self.assertTrue(result["renderValidation"]["identical"])

    def test_changed_first_pass_runs_and_passes_idempotence_check(self) -> None:
        original = _blank_pdf(tabs=False)

        repaired, result = prepare_pdf(original)

        self.assertNotEqual(repaired, original)
        self.assertTrue(result["secondPass"]["executed"])
        self.assertTrue(result["secondPass"]["byteStable"])
        self.assertEqual(result["secondPass"]["appliedRepairs"], [])
        self.assertTrue(result["renderValidation"]["identical"])
        self.assertEqual(run_sweeps(repaired)[0], repaired)

    def test_stage_telemetry_records_size_duration_and_changes(self) -> None:
        _repaired, result = run_sweeps(_blank_pdf(tabs=False))

        tab_order = result["stageTelemetry"]["tabOrder"]
        self.assertGreaterEqual(tab_order["durationMs"], 0)
        self.assertGreater(tab_order["inputBytes"], 0)
        self.assertGreater(tab_order["outputBytes"], 0)
        self.assertTrue(tab_order["changed"])
        self.assertIsNone(tab_order["error"])


if __name__ == "__main__":
    unittest.main()
