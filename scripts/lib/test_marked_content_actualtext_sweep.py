"""Regression tests for marked-content /ActualText repair (cd099d0a fixes).

After changing sweep code, run from repo root:
  ./scripts/run-a11y-tests.sh
"""

from __future__ import annotations

import io
import re
import unittest

from pathlib import Path

import pikepdf

from lib.marked_content_actualtext_sweep import (
    _get_mcid_block,
    _inject_actualtext_on_page,
    _read_page_contents,
    repair_marked_content_actualtext,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NUCLEIC_ACIDS_VALIDATION_PDF = (
    _REPO_ROOT
    / "qa-accessibility-handoff/ch1-topic-previews-acrobat-validation"
    / "04-Nucleic-Acids_preview_733277a2.pdf"
)


def _dual_mcid_page_stream() -> bytes:
    return (
        b"q /Figure<</MCID 15 /ActualText (stale wrong alt) >> BDC /Im1 Do EMC Q "
        b"q /Figure<</MCID 158 /ActualText (Colorized scanning electron micrograph of bacteria) >> BDC /Im2 Do EMC Q"
    )


def _build_dual_mcid_pdf() -> bytes:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    page["/Contents"] = pdf.make_stream(_dual_mcid_page_stream())
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _build_table_figure_struct_pdf() -> bytes:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    page["/Contents"] = pdf.make_stream(
        b"q /Figure<</MCID 8 >> BDC /Im1 Do EMC Q "
        b"q /Figure<</MCID 15 /ActualText (wrong bacteria alt) >> BDC /Im2 Do EMC Q"
    )

    figure = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Figure"),
            "/Alt": "Table",
            "/Contents": "Table",
            "/C": pikepdf.Array([pikepdf.Name("/table-figure-reverted")]),
            "/K": pikepdf.Array([8, 15]),
        }
    )
    document = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Document"),
            "/K": pikepdf.Array([figure]),
        }
    )
    pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array([document]),
        }
    )

    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _build_cd099d0a_regression_pdf() -> bytes:
    """cd099d0a-like page: table MCID 15 (StyleSpan) beside bacteria MCID 158."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    page["/Contents"] = pdf.make_stream(
        b"q /Figure<</MCID 8 >> BDC /Im1 Do EMC Q "
        b"q /StyleSpan<</MCID 15 /ActualText (wrong bacteria alt inherited) >> BDC /Im2 Do EMC Q "
        b"q /Figure<</MCID 158 /ActualText (Microscopic image of rod-shaped bacteria) >> BDC /Im3 Do EMC Q"
    )

    table_figure = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Figure"),
            "/Alt": "Table",
            "/Contents": "Table",
            "/C": pikepdf.Array([pikepdf.Name("/table-figure-reverted")]),
            "/K": pikepdf.Array([8, 15]),
        }
    )
    bacteria_figure = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Figure"),
            "/Alt": "Microscopic image of rod-shaped bacteria",
            "/K": pikepdf.Array([158]),
        }
    )
    document = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Document"),
            "/K": pikepdf.Array([table_figure, bacteria_figure]),
        }
    )
    pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array([document]),
        }
    )

    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _page_contents_text(pdf_bytes: bytes) -> str:
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        contents = pdf.pages[0].get("/Contents")
        assert contents is not None
        return _read_page_contents(contents).decode("latin1", errors="replace")


def _actualtext_for_mcid(contents: str, mcid: int) -> str | None:
    match = re.search(
        rf"/\w+<<[^>]*?/MCID\s+{mcid}(?!\d)[^>]*?/ActualText\s+\(([^)]*)\)",
        contents,
    )
    return match.group(1) if match else None


class McidBoundaryTests(unittest.TestCase):
    def test_get_mcid_block_distinguishes_15_from_158(self) -> None:
        data = _dual_mcid_page_stream()
        block_15 = _get_mcid_block(data, 15)
        block_158 = _get_mcid_block(data, 158)
        self.assertIsNotNone(block_15)
        self.assertIsNotNone(block_158)
        self.assertIn(b"/Im1 Do", block_15[1])
        self.assertIn(b"/Im2 Do", block_158[1])

    def test_get_mcid_block_does_not_match_prefix_mcid(self) -> None:
        data = b"/Figure<</MCID 158 /ActualText (bacteria) >> BDC /Im2 Do EMC"
        self.assertIsNone(_get_mcid_block(data, 15))


class InjectActualTextTests(unittest.TestCase):
    def test_inject_replaces_only_target_mcid(self) -> None:
        with pikepdf.open(io.BytesIO(_build_dual_mcid_pdf())) as pdf:
            page = pdf.pages[0]
            changed = _inject_actualtext_on_page(
                pdf,
                page,
                mcid=15,
                actual_text="Table",
                replace_existing=True,
            )
            self.assertTrue(changed)

            contents = _read_page_contents(page["/Contents"]).decode(
                "latin1", errors="replace"
            )
            self.assertEqual(_actualtext_for_mcid(contents, 15), "Table")
            self.assertIn("bacteria", _actualtext_for_mcid(contents, 158) or "")

    def test_inject_returns_false_when_actualtext_already_present(self) -> None:
        with pikepdf.open(io.BytesIO(_build_dual_mcid_pdf())) as pdf:
            page = pdf.pages[0]
            first = _inject_actualtext_on_page(
                pdf,
                page,
                mcid=158,
                actual_text="bacteria",
                replace_existing=False,
            )
            second = _inject_actualtext_on_page(
                pdf,
                page,
                mcid=158,
                actual_text="bacteria",
                replace_existing=False,
            )
            self.assertFalse(first)
            self.assertFalse(second)


class TableFigureRepairTests(unittest.TestCase):
    def test_repair_table_figure_sets_pg_and_table_actualtext(self) -> None:
        pdf_bytes = _build_table_figure_struct_pdf()
        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertGreater(result.mcids_updated, 0)
        self.assertTrue(any("figure1" in action for action in result.actions))

        contents = _page_contents_text(repaired)
        self.assertEqual(_actualtext_for_mcid(contents, 15), "Table")
        self.assertNotIn("bacteria", contents)

        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            struct_root = pdf.Root["/StructTreeRoot"]
            document = struct_root["/K"][0]
            figure = document["/K"][0]
            self.assertIsNotNone(figure.get("/Pg"))

    def test_repair_is_idempotent_on_synthetic_table_figure(self) -> None:
        pdf_bytes = _build_table_figure_struct_pdf()
        repaired_once, first = repair_marked_content_actualtext(pdf_bytes)
        repaired_twice, second = repair_marked_content_actualtext(repaired_once)
        self.assertGreater(first.mcids_updated, 0)
        self.assertEqual(second.mcids_updated, 0)
        self.assertEqual(second.actions, [])
        self.assertEqual(_page_contents_text(repaired_once), _page_contents_text(repaired_twice))


class Cd099d0aRegressionTests(unittest.TestCase):
    def test_table_mcid_15_does_not_steal_bacteria_mcid_158_alt(self) -> None:
        pdf_bytes = _build_cd099d0a_regression_pdf()
        repaired_once, first = repair_marked_content_actualtext(pdf_bytes)
        repaired_twice, second = repair_marked_content_actualtext(repaired_once)

        self.assertGreater(first.mcids_updated, 0)

        contents = _page_contents_text(repaired_once)
        self.assertEqual(_actualtext_for_mcid(contents, 15), "Table")
        bacteria_alt = _actualtext_for_mcid(contents, 158) or ""
        self.assertIn("bacteria", bacteria_alt.lower())
        self.assertNotIn("Table", bacteria_alt)

        self.assertEqual(second.mcids_updated, 0)
        self.assertEqual(second.actions, [])


class NucleicAcidsRegressionTests(unittest.TestCase):
    def test_repair_multi_mcid_table_does_not_bloat_content_stream(self) -> None:
        if not _NUCLEIC_ACIDS_VALIDATION_PDF.is_file():
            self.skipTest("733277a2 validation PDF not available")

        pdf_bytes = _NUCLEIC_ACIDS_VALIDATION_PDF.read_bytes()
        with pikepdf.open(io.BytesIO(pdf_bytes)) as before:
            before_len = len(_read_page_contents(before.pages[0]["/Contents"]))
        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        with pikepdf.open(io.BytesIO(repaired)) as after:
            after_len = len(_read_page_contents(after.pages[0]["/Contents"]))
        self.assertGreater(result.mcids_updated, 0)
        self.assertLess(after_len, before_len * 1.05)


if __name__ == "__main__":
    unittest.main()
