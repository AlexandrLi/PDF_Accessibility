"""Tests for the generic PDF tab-order sweep."""

from __future__ import annotations

import io
import unittest

import pikepdf

from lib.tab_order_sweep import repair_tab_order


def _make_pdf() -> bytes:
    pdf = pikepdf.new()
    tabs_values = ("/R", "/C", "/S", None)

    for page_number, tabs in enumerate(tabs_values, start=1):
        page = pdf.add_blank_page(page_size=(200, 200))
        page["/Contents"] = pdf.make_stream(
            f"BT /F1 12 Tf ({page_number}) Tj ET".encode("ascii")
        )
        if tabs is not None:
            page["/Tabs"] = pikepdf.Name(tabs)
        if page_number == 1:
            annotation = pdf.make_indirect(
                pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/Annot"),
                        "/Subtype": pikepdf.Name("/Text"),
                        "/Rect": pikepdf.Array([10, 10, 30, 30]),
                        "/Contents": pikepdf.String("preserve this annotation"),
                    }
                )
            )
            page["/Annots"] = pikepdf.Array([annotation])

    struct_element = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/P"),
                "/K": pikepdf.Array([0]),
            }
        )
    )
    pdf.Root["/StructTreeRoot"] = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([struct_element]),
            }
        )
    )
    pdf.Root["/CustomMarker"] = pikepdf.String("preserve this root entry")

    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


class TabOrderSweepTests(unittest.TestCase):
    def test_sets_tabs_s_on_multiple_pages_and_replaces_existing_values(self) -> None:
        repaired, result = repair_tab_order(_make_pdf())

        self.assertEqual(result.pages_found, 4)
        self.assertEqual(result.pages_updated, 3)
        self.assertEqual(len(result.actions), 3)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(len(pdf.pages), 4)
            for page in pdf.pages:
                self.assertEqual(page["/Tabs"], pikepdf.Name("/S"))

    def test_preserves_existing_s_and_structure_content_and_annotations(self) -> None:
        original = _make_pdf()
        with pikepdf.open(io.BytesIO(original)) as pdf:
            original_contents = [
                page["/Contents"].read_bytes() for page in pdf.pages
            ]
            original_annotation_contents = pdf.pages[0]["/Annots"][0]["/Contents"]
            original_marker = pdf.Root["/CustomMarker"]

        repaired, _result = repair_tab_order(original)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(
                [page["/Contents"].read_bytes() for page in pdf.pages],
                original_contents,
            )
            self.assertEqual(len(pdf.pages[0]["/Annots"]), 1)
            self.assertEqual(
                pdf.pages[0]["/Annots"][0]["/Contents"],
                original_annotation_contents,
            )
            self.assertEqual(pdf.Root["/StructTreeRoot"]["/K"][0]["/S"], "/P")
            self.assertEqual(pdf.Root["/StructTreeRoot"]["/K"][0]["/K"][0], 0)
            self.assertEqual(pdf.Root["/CustomMarker"], original_marker)
            self.assertEqual(pdf.pages[2]["/Tabs"], pikepdf.Name("/S"))

    def test_second_run_is_idempotent_and_output_remains_valid(self) -> None:
        repaired_once, _first = repair_tab_order(_make_pdf())
        repaired_twice, second = repair_tab_order(repaired_once)

        self.assertEqual(second.pages_updated, 0)
        self.assertEqual(second.actions, [])
        self.assertEqual(repaired_twice, repaired_once)
        with pikepdf.open(io.BytesIO(repaired_twice)) as pdf:
            self.assertEqual(len(pdf.pages), 4)
            self.assertTrue(
                all(page["/Tabs"] == pikepdf.Name("/S") for page in pdf.pages)
            )


if __name__ == "__main__":
    unittest.main()
