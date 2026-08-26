"""Tests for generic hierarchical PDF bookmark repair."""

from __future__ import annotations

import io
import unittest

import pikepdf

from lib.bookmark_sweep import repair_bookmarks


def _make_pdf(
    headings: list[tuple[int, str | None, int | None]],
    *,
    with_existing_outline: bool = False,
    malformed_outline: bool = False,
) -> bytes:
    pdf = pikepdf.new()
    pages = []
    for page_number in range(1, 4):
        page = pdf.add_blank_page(page_size=(200, 200))
        page["/Contents"] = pdf.make_stream(
            f"BT /F1 12 Tf (page {page_number}) Tj ET".encode("ascii")
        )
        pages.append(page)

    elements = []
    for index, (level, label, page_number) in enumerate(headings):
        element = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name(f"/H{level}"),
                "/Pg": pages[(page_number or 1) - 1].obj if page_number else None,
            }
        )
        if label is not None:
            element["/T"] = pikepdf.String(label)
        if page_number and label is None:
            element["/K"] = index
            contents = pages[page_number - 1]["/Contents"]
            contents.write(
                f"/H{level} << /MCID {index} >> BDC "
                f"BT /F1 12 Tf (fallback {index}) Tj ET EMC".encode("ascii")
            )
        elements.append(pdf.make_indirect(element))

    root = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array(elements),
            }
        )
    )
    pdf.Root["/StructTreeRoot"] = root

    if with_existing_outline:
        with pdf.open_outline() as outline:
            outline.root.append(
                pikepdf.OutlineItem("Authored bookmark", destination=0, page_location="Fit")
            )
    elif malformed_outline:
        item = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Title": pikepdf.String("Broken bookmark"),
                    "/Dest": pikepdf.Array([pages[0].obj, pikepdf.Name("/Fit")]),
                }
            )
        )
        pdf.Root["/Outlines"] = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/Outlines"),
                    "/First": item,
                    "/Count": 1,
                }
            )
        )

    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


def _top_level_items(pdf: pikepdf.Pdf) -> list[pikepdf.Dictionary]:
    root = pdf.Root["/Outlines"]
    items = []
    item = root.get("/First")
    while item is not None:
        items.append(item)
        item = item.get("/Next")
    return items


class BookmarkSweepTests(unittest.TestCase):
    def test_builds_nested_h1_through_h6_hierarchy(self) -> None:
        original = _make_pdf([(level, f"H{level}", 1) for level in range(1, 7)])

        repaired, result = repair_bookmarks(original)

        self.assertEqual(result.headings_found, 6)
        self.assertEqual(result.bookmarks_created, 6)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            item = _top_level_items(pdf)[0]
            for level in range(1, 7):
                self.assertEqual(item["/Title"], f"H{level}")
                self.assertEqual(len(item["/Dest"]), 2)
                self.assertEqual(item["/Dest"][0].objgen, pdf.pages[0].objgen)
                if level < 6:
                    item = item["/First"]
            self.assertEqual(pdf.Root["/PageMode"], pikepdf.Name("/UseOutlines"))

    def test_skipped_levels_attach_to_nearest_lower_level(self) -> None:
        repaired, _result = repair_bookmarks(
            _make_pdf(
                [
                    (1, "One", 1),
                    (3, "Three", 1),
                    (4, "Four", 1),
                    (2, "Two", 2),
                    (6, "Six", 3),
                ]
            )
        )

        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            top = _top_level_items(pdf)
            self.assertEqual([str(item["/Title"]) for item in top], ["One"])
            self.assertEqual(str(top[0]["/First"]["/Title"]), "Three")
            self.assertEqual(str(top[0]["/First"]["/First"]["/Title"]), "Four")
            self.assertEqual(str(top[0]["/First"]["/Next"]["/Title"]), "Two")
            self.assertEqual(
                str(top[0]["/First"]["/Next"]["/First"]["/Title"]),
                "Six",
            )

    def test_uses_reliable_label_fallback_and_skips_unlabeled_heading(self) -> None:
        repaired, result = repair_bookmarks(
            _make_pdf([(1, None, 1), (2, None, None)])
        )

        self.assertEqual(result.bookmarks_created, 1)
        self.assertEqual(len(result.headings_skipped), 1)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(str(_top_level_items(pdf)[0]["/Title"]), "fallback 0")

    def test_uses_heading_metadata_in_priority_order(self) -> None:
        original = _make_pdf([(1, "", 1)])
        with pikepdf.open(io.BytesIO(original)) as pdf:
            heading = pdf.Root["/StructTreeRoot"]["/K"][0]
            heading["/ActualText"] = pikepdf.String("actual label")
            heading["/Alt"] = pikepdf.String("alternate label")
            heading["/Contents"] = pikepdf.String("contents label")
            output = io.BytesIO()
            pdf.save(output)
            original = output.getvalue()

        repaired, _result = repair_bookmarks(original)

        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(str(_top_level_items(pdf)[0]["/Title"]), "actual label")

    def test_existing_nonempty_outline_is_preserved(self) -> None:
        original = _make_pdf(
            [(1, "Derived heading", 1)],
            with_existing_outline=True,
        )

        repaired, result = repair_bookmarks(original)

        self.assertEqual(repaired, original)
        self.assertTrue(result.existing_outline_preserved)
        self.assertEqual(result.existing_outline_status, "valid")
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(str(_top_level_items(pdf)[0]["/Title"]), "Authored bookmark")

    def test_malformed_outline_is_reported_without_replacement(self) -> None:
        original = _make_pdf(
            [(1, "Derived heading", 1)],
            malformed_outline=True,
        )

        repaired, result = repair_bookmarks(original)

        self.assertEqual(repaired, original)
        self.assertEqual(result.existing_outline_status, "malformed")
        self.assertTrue(result.conflicts)

    def test_preserves_page_content_and_second_run_is_idempotent(self) -> None:
        original = _make_pdf([(1, "Heading", 2)])
        with pikepdf.open(io.BytesIO(original)) as pdf:
            original_contents = [page["/Contents"].read_bytes() for page in pdf.pages]

        repaired_once, _first = repair_bookmarks(original)
        repaired_twice, second = repair_bookmarks(repaired_once)

        self.assertEqual(repaired_twice, repaired_once)
        self.assertEqual(second.existing_outline_status, "valid")
        self.assertEqual(second.bookmarks_created, 0)
        with pikepdf.open(io.BytesIO(repaired_once)) as pdf:
            self.assertEqual(
                [page["/Contents"].read_bytes() for page in pdf.pages],
                original_contents,
            )


if __name__ == "__main__":
    unittest.main()
