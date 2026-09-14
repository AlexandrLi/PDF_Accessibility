"""Tests for the document title sweep."""

from __future__ import annotations

import io
import unittest

import pikepdf

from lib.document_title_sweep import repair_document_title


def _make_pdf(*, title: str | None = None, display: bool | None = None) -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page["/Contents"] = pdf.make_stream(b"BT /F1 12 Tf (page one) Tj ET")
    if title is not None:
        pdf.docinfo["/Title"] = title
    if display is not None:
        pdf.Root["/ViewerPreferences"] = pdf.make_indirect(
            pikepdf.Dictionary({"/DisplayDocTitle": display})
        )
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


def _read(pdf_bytes: bytes) -> tuple[str, object]:
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        title = str(pdf.trailer.get("/Info", {}).get("/Title") or "")
        displayed = pdf.Root.get("/ViewerPreferences", {}).get("/DisplayDocTitle")
        return title, displayed


class DocumentTitleSweepTests(unittest.TestCase):
    def test_names_an_untitled_document_and_displays_it(self) -> None:
        repaired, result = repair_document_title(
            _make_pdf(display=True),
            title="Algebraic Expressions",
        )

        title, displayed = _read(repaired)
        self.assertEqual(title, "Algebraic Expressions")
        self.assertIs(displayed, True)
        self.assertEqual(result.title_after, "Algebraic Expressions")

    def test_sets_display_doc_title_when_absent(self) -> None:
        repaired, result = repair_document_title(
            _make_pdf(title="Dot Product"),
            title="Dot Product",
        )

        _title, displayed = _read(repaired)
        self.assertIs(displayed, True)
        self.assertFalse(result.display_doc_title_before)
        self.assertEqual(result.title_after, "Dot Product")

    def test_keeps_an_existing_title(self) -> None:
        _repaired, result = repair_document_title(
            _make_pdf(title="Author's own title", display=True),
            title="Course TOC title",
        )

        self.assertEqual(result.title_after, "Author's own title")
        self.assertEqual(result.actions, [])

    def test_a_repaired_document_is_untouched_by_a_second_pass(self) -> None:
        repaired, _result = repair_document_title(
            _make_pdf(),
            title="Exponents",
        )

        self.assertEqual(repair_document_title(repaired, title="Exponents")[0], repaired)

    def test_without_a_title_nothing_is_written(self) -> None:
        original = _make_pdf()

        repaired, result = repair_document_title(original)

        self.assertEqual(repaired, original)
        self.assertEqual(result.actions, ["no title supplied"])


if __name__ == "__main__":
    unittest.main()
