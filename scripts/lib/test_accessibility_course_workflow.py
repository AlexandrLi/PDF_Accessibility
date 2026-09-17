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


def _pdf_with_detached_owner() -> bytes:
    """A page whose only tagged block is owned from outside the tree.

    MCID 1's element names /Document as its parent but the /Document does not
    list it, so until taggedAnnotations reconnects it, taggedContent can infer
    no position for the untagged MCID 0 beside it.
    """
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page["/Contents"] = pdf.make_stream(
        " ".join(
            f"/P << /MCID {mcid} >> BDC "
            f"BT /F1 12 Tf 10 {180 - mcid * 20} Td (Line {mcid}) Tj ET EMC"
            for mcid in (0, 1)
        ).encode("ascii")
    )
    page["/StructParents"] = 0

    owner = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/P"),
                "/Pg": page.obj,
                "/K": 1,
            }
        )
    )
    document = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Document"),
                "/K": pikepdf.Array([]),
            }
        )
    )
    owner["/P"] = document
    root = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([document]),
            }
        )
    )
    root["/ParentTree"] = pdf.make_indirect(
        pikepdf.Dictionary({"/Nums": pikepdf.Array([0, pikepdf.Array([owner])])})
    )
    document["/P"] = root
    pdf.Root["/StructTreeRoot"] = root
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

    def test_reconnected_subtree_is_adopted_in_the_first_pass(self) -> None:
        # taggedAnnotations runs before taggedContent so that the blocks a
        # reconnected subtree makes placeable are adopted while taggedContent
        # is still running; the other order left them to a second pass and no
        # topic in the file was ever byte-stable.
        repaired, result = prepare_pdf(_pdf_with_detached_owner())

        self.assertTrue(result["secondPass"]["byteStable"])
        self.assertEqual(result["secondPass"]["appliedRepairs"], [])
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
