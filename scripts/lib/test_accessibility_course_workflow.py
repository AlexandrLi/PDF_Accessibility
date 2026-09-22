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



def _pdf_with_dead_figure_beside_an_orphan() -> bytes:
    """A page where the orphan block's only bracket is a Figure nobody reaches.

    The tree lists a paragraph owning MCID 1, an empty /Sect and a paragraph
    owning MCID 0, in that order. MCID 2 is an untagged paragraph and MCID 3 a
    Figure block whose element hangs under an abandoned /TD of that Sect, so
    the ParentTree names it but nothing reaches it. Until the Figure is back
    inside the Sect, the first sibling after MCID 1 that covers anything
    covers MCID 0, and taggedContent can infer no position for MCID 2.
    """
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    blocks = [
        f"/P << /MCID {mcid} >> BDC BT /F1 12 Tf 10 {180 - mcid * 20} Td "
        f"(Line {mcid}) Tj ET EMC"
        for mcid in (0, 1, 2)
    ]
    blocks.append(
        "/Figure << /MCID 3 >> BDC BT /F1 12 Tf 10 100 Td (y = 2x) Tj ET EMC"
    )
    page["/Contents"] = pdf.make_stream(" ".join(blocks).encode("ascii"))
    page["/StructParents"] = 0

    def element(kind: str, **entries: object) -> pikepdf.Dictionary:
        return pdf.make_indirect(
            pikepdf.Dictionary(
                {"/Type": pikepdf.Name("/StructElem"), "/S": pikepdf.Name(kind)}
                | entries
            )
        )

    paragraph_b = element("/P", **{"/Pg": page.obj, "/K": 0})
    paragraph_a = element("/P", **{"/Pg": page.obj, "/K": 1})
    section = element("/Sect", **{"/K": pikepdf.Array([])})
    document = element(
        "/Document", **{"/K": pikepdf.Array([paragraph_a, section, paragraph_b])}
    )
    root = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([document]),
            }
        )
    )
    document["/P"] = root
    paragraph_a["/P"] = document
    paragraph_b["/P"] = document
    section["/P"] = document

    abandoned_cell = element("/TD", **{"/P": section, "/K": pikepdf.Array([])})
    figure = element(
        "/Figure",
        **{
            "/Pg": page.obj,
            "/K": 3,
            "/P": abandoned_cell,
            "/Alt": "Graph of the line y equals 2x through the origin",
        },
    )
    root["/ParentTree"] = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Nums": pikepdf.Array(
                    [0, pikepdf.Array([paragraph_b, paragraph_a, None, figure])]
                )
            }
        )
    )
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

    def test_dead_figure_is_reattached_before_orphans_are_placed(self) -> None:
        # The Figure reattachment used to run inside markedContentActualText,
        # after taggedContent, so the block beside it was placed only by the
        # second pass and a resolved topic was held as not byte-stable.
        repaired, result = prepare_pdf(_pdf_with_dead_figure_beside_an_orphan())

        self.assertTrue(result["stageTelemetry"]["deadFigureOwners"]["changed"])
        self.assertIn("taggedContent", result["appliedRepairs"])
        self.assertTrue(result["secondPass"]["byteStable"])
        self.assertEqual(result["secondPass"]["appliedRepairs"], [])
        self.assertEqual(run_sweeps(repaired)[0], repaired)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            document = pdf.Root.StructTreeRoot.K[0]
            kinds = [str(kid.S) for kid in document.K]
            owned = [
                kid.K if isinstance(kid.K, int) else None for kid in document.K
            ]
        self.assertEqual(kinds, ["/P", "/P", "/Sect", "/P"])
        self.assertEqual(owned, [1, 2, None, 0])


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
