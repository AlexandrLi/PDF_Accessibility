"""Regression tests for marked-content /ActualText repair (cd099d0a fixes).

After changing sweep code, run from repo root:
  ./scripts/run-a11y-tests.sh
"""

from __future__ import annotations

import io
import re
import unittest
import unittest.mock

from pathlib import Path

import pikepdf

from lib.test_glyph_evidence import MINUS_GID, MINUS_RECORD, _truetype_program
from lib.marked_content_actualtext_sweep import (
    FontState,
    _decode_pdf_actualtext_value,
    _decode_label_mcid_text,
    _decode_shown_text_in_order,
    _font_in_effect_at,
    _get_mcid_block,
    _inject_actualtext_in_data,
    _inject_actualtext_on_page,
    _mcid_bdc_has_actualtext,
    _page_font_code_maps,
    _pdf_literal_string,
    _read_actualtext_from_mcid,
    _read_page_contents,
    _resolve_struct_page,
    _set_struct_page_if_missing,
    _spoken_list_label_text,
    _tounicode_text,
    count_li_lbl_missing_actualtext,
    count_orphan_marked_missing_actualtext,
    list_nested_alternate_text,
    list_untagged_image_mcids_missing_actualtext,
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


def _type0_font(
    pdf: pikepdf.Pdf, bfchars: dict[str, str], program: bytes | None = None
) -> pikepdf.Dictionary:
    """An Identity-H font whose /ToUnicode names the given codes.

    With a program, the font embeds it as a CIDFontType2 with identity
    glyph ids, the shape Word gives Cambria Math.
    """
    entries = "\n".join(f"<{src}> <{dst}>" for src, dst in bfchars.items())
    cmap = (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(bfchars)} beginbfchar\n{entries}\nendbfchar\n"
        "endcmap end end"
    )
    font = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/Font"),
            "/Subtype": pikepdf.Name("/Type0"),
            "/BaseFont": pikepdf.Name("/ArialMT"),
            "/Encoding": pikepdf.Name("/Identity-H"),
            "/ToUnicode": pdf.make_stream(cmap.encode("latin1")),
        }
    )
    if program is not None:
        font["/BaseFont"] = pikepdf.Name("/ABCDEF+CambriaMath")
        font["/DescendantFonts"] = pikepdf.Array(
            [
                pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/Font"),
                        "/Subtype": pikepdf.Name("/CIDFontType2"),
                        "/CIDToGIDMap": pikepdf.Name("/Identity"),
                        "/FontDescriptor": pikepdf.Dictionary(
                            {"/FontFile2": pdf.make_stream(program)}
                        ),
                    }
                )
            ]
        )
    return pdf.make_indirect(font)


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

    def test_inject_rebuilds_clean_block_when_actualtext_literal_is_corrupt(self) -> None:
        garbage = b"pikepdf.Dictionary({'/ActualText': 'Table'})" * 200
        data = (
            b"/StyleSpan<< /MCID 44 /ActualText ("
            + garbage
            + b") >> BDC /Im1 Do EMC"
        )
        new_data, changed = _inject_actualtext_in_data(
            data,
            mcid=44,
            actual_text="Table",
            replace_existing=True,
        )
        self.assertTrue(changed)
        self.assertEqual(
            new_data,
            b"/StyleSpan<< /MCID 44 /ActualText (Table) >> BDC /Im1 Do EMC",
        )


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
        self.assertEqual(repaired_twice, repaired_once)
        self.assertEqual(_page_contents_text(repaired_once), _page_contents_text(repaired_twice))


def _build_shared_mcid_conflict_pdf() -> bytes:
    """d1e08a1e-like page: a table-figure-reverted element and a real figure
    with an authoritative /Alt both claim the same MCID 31."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    page["/Contents"] = pdf.make_stream(
        b"q /Figure<</MCID 31 >> BDC /Im1 Do EMC Q"
    )

    table_figure = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Figure"),
            "/Alt": "Table",
            "/Contents": "Table",
            "/C": pikepdf.Array([pikepdf.Name("/table-figure-reverted")]),
            "/K": pikepdf.Array([31]),
        }
    )
    real_figure = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Figure"),
            "/Alt": "Illustration of a therapy session.",
            "/K": pikepdf.Array([31]),
        }
    )
    document = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Document"),
            "/K": pikepdf.Array([real_figure, table_figure]),
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


class SharedMcidAltPrecedenceConflictTests(unittest.TestCase):
    def test_table_figure_does_not_inject_on_alt_protected_mcid(self) -> None:
        repaired, result = repair_marked_content_actualtext(
            _build_shared_mcid_conflict_pdf()
        )
        contents = _page_contents_text(repaired)
        self.assertIsNone(_actualtext_for_mcid(contents, 31))
        self.assertFalse(
            any("stripped duplicate" in action for action in result.actions)
        )
        self.assertFalse(
            any("for table figure" in action for action in result.actions)
        )

    def test_repair_is_byte_stable_on_shared_mcid_conflict(self) -> None:
        repaired_once, _ = repair_marked_content_actualtext(
            _build_shared_mcid_conflict_pdf()
        )
        repaired_twice, second = repair_marked_content_actualtext(repaired_once)
        self.assertEqual(repaired_twice, repaired_once)
        self.assertFalse(
            any("stripped duplicate" in action for action in second.actions)
        )


_PREFIX_TABLE_ALT = (
    "Table of prefixes used to provide details on numbers, sizes or amounts. "
    "The prefix 'mono-, uni-' is defined as 'one'."
)


def _build_described_and_placeholder_table_figures_pdf() -> bytes:
    """376b9c8a-like page: a table Figure with a descriptive /Alt and a
    table-figure-reverted placeholder both claim MCID 25."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    page["/Contents"] = pdf.make_stream(
        b"q /Figure<</MCID 9 >> BDC /Im1 Do EMC Q "
        b"q /Figure<</MCID 25 >> BDC /Im2 Do EMC Q"
    )

    described_figure = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Figure"),
            "/Alt": _PREFIX_TABLE_ALT,
            "/K": pikepdf.Array([25]),
        }
    )
    placeholder_figure = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Figure"),
            "/Alt": "Table",
            "/Contents": "Table",
            "/C": pikepdf.Array([pikepdf.Name("/table-figure-reverted")]),
            "/K": pikepdf.Array([9, 25]),
        }
    )
    document = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Document"),
            "/K": pikepdf.Array([described_figure, placeholder_figure]),
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


class DescribedTableFigureSharedMcidTests(unittest.TestCase):
    def test_placeholder_table_figure_keeps_described_actualtext(self) -> None:
        repaired, _ = repair_marked_content_actualtext(
            _build_described_and_placeholder_table_figures_pdf()
        )
        contents = _page_contents_text(repaired)
        self.assertEqual(_actualtext_for_mcid(contents, 25), _PREFIX_TABLE_ALT)
        self.assertEqual(_actualtext_for_mcid(contents, 9), "Table")

    def test_repair_is_byte_stable_with_described_and_placeholder_figures(self) -> None:
        repaired_once, _ = repair_marked_content_actualtext(
            _build_described_and_placeholder_table_figures_pdf()
        )
        repaired_twice, second = repair_marked_content_actualtext(repaired_once)
        self.assertEqual(repaired_twice, repaired_once)
        self.assertEqual(second.actions, [])


class Cd099d0aRegressionTests(unittest.TestCase):
    def test_table_mcid_15_does_not_steal_bacteria_mcid_158_alt(self) -> None:
        pdf_bytes = _build_cd099d0a_regression_pdf()
        repaired_once, first = repair_marked_content_actualtext(pdf_bytes)
        repaired_twice, second = repair_marked_content_actualtext(repaired_once)

        self.assertGreater(first.mcids_updated, 0)

        contents = _page_contents_text(repaired_once)
        self.assertEqual(_actualtext_for_mcid(contents, 15), "Table")
        with pikepdf.open(io.BytesIO(repaired_once)) as opened:
            document = opened.Root["/StructTreeRoot"]["/K"][0]
            bacteria_figure = document["/K"][1]
            bacteria_alt = str(bacteria_figure.get("/Alt") or "")
        self.assertIn("bacteria", bacteria_alt.lower())
        self.assertNotIn("Table", bacteria_alt)

        self.assertEqual(second.mcids_updated, 0)
        self.assertEqual(second.actions, [])


class SaltingOutRegressionTests(unittest.TestCase):
    def test_repair_nested_li_resolves_page_and_adds_figure_actualtext(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"q /Figure<</MCID 26 >> BDC /Im0 Do EMC Q "
            b"q /Lbl<</MCID 6 >> BDC /Im1 Do EMC Q "
            b"q /LBody<</MCID 7 >> BDC (Step 1) Tj EMC Q"
        )
        struct_root = pikepdf.Dictionary(
            Type=pikepdf.Name("/StructTreeRoot"),
            K=pikepdf.Array(
                [
                    pikepdf.Dictionary(
                        S=pikepdf.Name("/Document"),
                        K=pikepdf.Array(
                            [
                                pikepdf.Dictionary(
                                    S=pikepdf.Name("/Figure"),
                                    Alt=pikepdf.String("Salting diagram"),
                                    K=26,
                                ),
                                pikepdf.Dictionary(
                                    S=pikepdf.Name("/L"),
                                    K=pikepdf.Array(
                                        [
                                            pikepdf.Dictionary(
                                                S=pikepdf.Name("/LI"),
                                                K=pikepdf.Dictionary(
                                                    S=pikepdf.Name("/LI"),
                                                    K=pikepdf.Array(
                                                        [
                                                            pikepdf.Dictionary(
                                                                S=pikepdf.Name("/Lbl"),
                                                                K=6,
                                                            ),
                                                            pikepdf.Dictionary(
                                                                S=pikepdf.Name("/LBody"),
                                                                K=7,
                                                            ),
                                                        ]
                                                    ),
                                                ),
                                            )
                                        ]
                                    ),
                                ),
                            ]
                        ),
                    )
                ]
            ),
        )
        pdf.Root["/StructTreeRoot"] = struct_root
        buf = io.BytesIO()
        pdf.save(buf)
        pdf_bytes = buf.getvalue()

        with pikepdf.open(io.BytesIO(pdf_bytes)) as opened:
            li = opened.Root["/StructTreeRoot"]["/K"][0]["/K"][1]["/K"][0]
            self.assertIsNotNone(_resolve_struct_page(opened, li))

        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertGreater(result.mcids_updated, 0)
        contents = _page_contents_text(repaired)
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            figure = opened.Root["/StructTreeRoot"]["/K"][0]["/K"][0]
            self.assertEqual(str(figure.get("/Alt")), "Salting diagram")
        self.assertTrue(_actualtext_for_mcid(contents, 6))


class ExtraCharSpanNestedAltRegressionTests(unittest.TestCase):
    def test_repair_li_alt_and_extra_char_span(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"/LI<</MCID 10 /ActualText (Step 4) >> BDC q /Im0 Do EMC "
            b"/LBody<</MCID 11 >> BDC (Hb State ) Tj EMC "
            b"/ExtraCharSpan<</MCID 12 >> BDC <00C6> Tj EMC "
            b"/LBody<</MCID 13 >> BDC (Hb State) Tj EMC EMC"
        )

        extra_span = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/ExtraCharSpan"),
                "/ActualText": "→ ",
                "/Pg": page.obj,
                "/K": 12,
            }
        )
        lbody1 = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LBody"),
                "/Pg": page.obj,
                "/K": 11,
            }
        )
        lbody2 = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LBody"),
                "/Pg": page.obj,
                "/K": 13,
            }
        )
        li = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LI"),
                "/Alt": "Step 4",
                "/Pg": page.obj,
                "/K": pikepdf.Array([10, lbody1, extra_span, lbody2]),
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([li]),
            }
        )

        buf = io.BytesIO()
        pdf.save(buf)
        repaired, result = repair_marked_content_actualtext(buf.getvalue())
        actions = "\n".join(result.actions)
        contents = _page_contents_text(repaired)

        self.assertIn(
            "removed /Alt from /LI whose descendants carry their own alternate text",
            actions,
        )
        self.assertIn("removed struct /ActualText from ExtraCharSpan", actions)
        self.assertIn("retagged struct ExtraCharSpan to Span", actions)
        self.assertIn("/Span<< /MCID 12", contents)
        self.assertNotIn("/ExtraCharSpan", contents)
        self.assertIn("/LI<</MCID 10 /ActualText", contents)

        with pikepdf.open(io.BytesIO(repaired)) as opened:
            li_elem = opened.Root["/StructTreeRoot"]["/K"][0]
            self.assertIsNone(li_elem.get("/Alt"))
            span_elem = li_elem["/K"][2]
            self.assertEqual(span_elem.get("/S"), pikepdf.Name("/Span"))
            self.assertIsNone(span_elem.get("/ActualText"))


class NestedFigureAltRegressionTests(unittest.TestCase):
    def test_preserves_figure_backed_by_form_xobject(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"q /Figure<</MCID 4 >> BDC /Fm0 Do EMC Q"
        )
        figure = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Figure"),
                "/Alt": "Derivative graph",
                "/Pg": page.obj,
                "/K": 4,
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([figure]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)
        original = buf.getvalue()

        repaired, result = repair_marked_content_actualtext(original)

        self.assertEqual(repaired, original)
        self.assertNotIn("converted non-image Figure", "\n".join(result.actions))

    def test_figure_alt_precedence_skips_shared_list_image_mcid(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"q /Figure<</MCID 9 >> BDC /Im0 Do EMC Q"
        )
        figure = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Figure"),
                "/Alt": "Wavelength diagram",
                "/Pg": page.obj,
                "/K": 9,
            }
        )
        li = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LI"),
                "/Pg": page.obj,
                "/K": pikepdf.Array([9]),
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([figure, li]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)
        original = buf.getvalue()

        repaired, result = repair_marked_content_actualtext(original)

        self.assertEqual(repaired, original)
        self.assertEqual(result.mcids_updated, 0)
        self.assertEqual(result.actions, [])

    def test_preserves_ambiguous_mixed_figure_linkage(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"q /Figure<</MCID 10 /ActualText (Main diagram) >> BDC /Im0 Do "
            b"/Figure<</MCID 11 >> BDC (label) Tj EMC EMC Q"
        )

        figure = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Figure"),
                "/Alt": "Main diagram",
                "/Pg": page.obj,
                "/K": pikepdf.Array([10, 11]),
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
        repaired, result = repair_marked_content_actualtext(buf.getvalue())
        contents = _page_contents_text(repaired)

        self.assertNotIn("converted non-image Figure", "\n".join(result.actions))
        self.assertIn("stripped duplicate /ActualText", "\n".join(result.actions))
        self.assertNotIn("/Span<< /MCID 11", contents)
        self.assertIn("/Figure<</MCID 11", contents)
        self.assertNotIn("/Figure<< /MCID 10 /ActualText", contents)

        with pikepdf.open(io.BytesIO(repaired)) as opened:
            root = opened.Root["/StructTreeRoot"]
            figure_elem = root["/K"][0]["/K"][0]
            self.assertEqual(figure_elem.get("/S"), pikepdf.Name("/Figure"))
            self.assertEqual(list(figure_elem.get("/K", [])), [10, 11])
            self.assertEqual(str(figure_elem.get("/Alt")), "Main diagram")


class FigureDuplicateContentsRegressionTests(unittest.TestCase):
    def test_remove_contents_when_figure_has_alt(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        figure = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Figure"),
                "/Alt": "Arrow diagram",
                "/Contents": "Arrow diagram",
                "/Pg": page.obj,
                "/K": pikepdf.Array([1]),
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([figure]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)
        repaired, result = repair_marked_content_actualtext(buf.getvalue())
        self.assertIn("removed duplicate /Contents", "\n".join(result.actions))
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            fig = opened.Root["/StructTreeRoot"]["/K"][0]
            self.assertEqual(str(fig.get("/Alt")), "Arrow diagram")
            self.assertIsNone(fig.get("/Contents"))


class ResolveStructPageTests(unittest.TestCase):
    def test_resolve_lbl_prefers_page_with_matching_bdc_tag(self) -> None:
        pdf = pikepdf.Pdf.new()
        page0 = pdf.add_blank_page()
        page1 = pdf.add_blank_page()
        page0["/Contents"] = pdf.make_stream(
            b"q /Lbl<</MCID 13 >> BDC <0191>Tj EMC Q"
        )
        page1["/Contents"] = pdf.make_stream(
            b"q /Figure<</MCID 13 >> BDC /Im1 Do EMC Q"
        )
        lbl = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Lbl"),
                "/Pg": page1.obj,
                "/K": 13,
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([lbl]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)

        with pikepdf.open(io.BytesIO(buf.getvalue())) as opened:
            lbl_elem = opened.Root["/StructTreeRoot"]["/K"][0]
            resolved = _resolve_struct_page(opened, lbl_elem)
            self.assertEqual(resolved.objgen, opened.pages[0].objgen)

    def test_resolve_li_does_not_use_tag_matching(self) -> None:
        pdf = pikepdf.Pdf.new()
        page0 = pdf.add_blank_page()
        page1 = pdf.add_blank_page()
        page0["/Contents"] = pdf.make_stream(
            b"q /Lbl<</MCID 5 >> BDC (a) Tj EMC "
            b"q /LBody<</MCID 6 >> BDC (body) Tj EMC Q"
        )
        page1["/Contents"] = pdf.make_stream(
            b"q /Lbl<</MCID 5 >> BDC (x) Tj EMC "
            b"q /LBody<</MCID 6 >> BDC (wrong) Tj EMC Q"
        )
        li = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LI"),
                "/Pg": page1.obj,
                "/K": pikepdf.Array([5, 6]),
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([li]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)

        with pikepdf.open(io.BytesIO(buf.getvalue())) as opened:
            li_elem = opened.Root["/StructTreeRoot"]["/K"][0]
            resolved = _resolve_struct_page(opened, li_elem)
            self.assertEqual(resolved.objgen, opened.pages[1].objgen)


    def test_resolve_honours_pg_when_parent_tree_names_the_element(self) -> None:
        # ee112303 regression: a Span owns a /Lbl-tagged image block on page 1,
        # and page 2 happens to tag the same MCID as /Span. Tag matching alone
        # sent the element to page 2, so its image never got alternate text.
        pdf = pikepdf.Pdf.new()
        page0 = pdf.add_blank_page()
        page1 = pdf.add_blank_page()
        page0["/Contents"] = pdf.make_stream(
            b"q /Lbl<</MCID 117 >> BDC /Im2 Do EMC Q"
        )
        page1["/Contents"] = pdf.make_stream(
            b"q /Span<</MCID 117 >> BDC (later) Tj EMC Q"
        )
        span = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/Span"),
                    "/Pg": page0.obj,
                    "/K": 117,
                }
            )
        )
        page0["/StructParents"] = 0
        page1["/StructParents"] = 1
        nums0 = pikepdf.Array([None] * 117 + [span])
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([span]),
                "/ParentTree": pikepdf.Dictionary(
                    {"/Nums": pikepdf.Array([0, nums0, 1, pikepdf.Array([])])}
                ),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)

        with pikepdf.open(io.BytesIO(buf.getvalue())) as opened:
            span_elem = opened.Root["/StructTreeRoot"]["/K"][0]
            resolved = _resolve_struct_page(opened, span_elem)
            self.assertEqual(resolved.objgen, opened.pages[0].objgen)


class ListItemLabelActualTextTests(unittest.TestCase):
    def test_repair_list_item_label_actualtext(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"q /Lbl<</MCID 5 >> BDC (c\\)   )Tj EMC "
            b"q /Lbl<</MCID 7 >> BDC BT /TT0 1 Tf [(a)-3 (\\)   )]TJ ET EMC "
            b"q /LBody<</MCID 6 >> BDC (Answer text) Tj EMC Q"
        )
        lbl_c = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Lbl"),
                "/K": 5,
                "/Pg": page.obj,
            }
        )
        lbl_a = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Lbl"),
                "/K": 7,
                "/Pg": page.obj,
            }
        )
        lbody = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LBody"),
                "/K": 6,
                "/Pg": page.obj,
            }
        )
        li = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LI"),
                "/Pg": page.obj,
                "/K": pikepdf.Array([lbl_c, lbody, lbl_a]),
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([li]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)
        pdf_bytes = buf.getvalue()

        self.assertEqual(count_li_lbl_missing_actualtext(pdf_bytes), 2)
        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertGreater(result.mcids_updated, 0)
        self.assertEqual(count_li_lbl_missing_actualtext(repaired), 0)

        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertTrue(_mcid_bdc_has_actualtext(data, 5))
            self.assertTrue(_mcid_bdc_has_actualtext(data, 7))
            li_elem = opened.Root["/StructTreeRoot"]["/K"][0]
            lbl_elems = [kid for kid in li_elem["/K"] if str(kid.get("/S")) == "/Lbl"]
            spoken = sorted(str(lbl.get("/ActualText")) for lbl in lbl_elems)
            self.assertEqual(spoken, ["option a", "option c"])

    def test_list_item_inline_image_gets_generic_label(self) -> None:
        # The item's own text is already spoken as text; the inline image
        # must get the short generic label, never an echo of that text.
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page(page_size=(200, 200))
        image = pikepdf.Stream(pdf, b"\xff")
        image["/Type"] = pikepdf.Name("/XObject")
        image["/Subtype"] = pikepdf.Name("/Image")
        image["/Width"] = 1
        image["/Height"] = 1
        image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
        image["/BitsPerComponent"] = 8
        page["/Resources"] = pikepdf.Dictionary(
            {"/XObject": pikepdf.Dictionary({"/Im1": image})}
        )
        page["/Contents"] = pdf.make_stream(
            b"/LBody<</MCID 1>> BDC (Gene expression is controlled!) Tj EMC "
            b"/LBody<</MCID 2>> BDC q 20 0 0 20 5 5 cm /Im1 Do Q EMC"
        )
        text_body = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LBody"),
                "/K": pikepdf.Array([1, 2]),
                "/Pg": page.obj,
            }
        )
        li = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LI"),
                "/Pg": page.obj,
                "/K": pikepdf.Array([text_body]),
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([li]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)

        repaired, result = repair_marked_content_actualtext(buf.getvalue())
        self.assertIn("list item 1: added /ActualText to image MCIDs [2]",
                      "\n".join(result.actions))
        contents = _page_contents_text(repaired)
        self.assertEqual(_actualtext_for_mcid(contents, 2), "Figure")
        self.assertNotIn("increased", contents)
        self.assertNotIn("Gene expression is controlled! Figure", contents)

    def test_spoken_list_label_text_normalized_only(self) -> None:
        self.assertEqual(
            _spoken_list_label_text("a)  ", normalized_only=True), "option a"
        )
        self.assertEqual(_spoken_list_label_text("□", normalized_only=True), "blank")
        self.assertEqual(_spoken_list_label_text("___", normalized_only=True), "blank")
        # Raw content-stream bytes from a symbolic font are glyph codes,
        # not text; the label repair must not echo them as spoken text.
        self.assertIsNone(_spoken_list_label_text('"#', normalized_only=True))
        self.assertIsNone(_spoken_list_label_text("1.", normalized_only=True))
        self.assertEqual(_spoken_list_label_text('"#'), '"#')

    def test_label_glyph_code_is_read_through_the_font(self) -> None:
        # 0194 is a bullet in this font. Naming the code beats the
        # blank-square convention, or the bullet is spoken "blank".
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Resources"] = pikepdf.Dictionary(
            {"/Font": pikepdf.Dictionary({"/C2_1": _type0_font(pdf, {"0194": "25CF"})})}
        )
        body = b"q BT /C2_1 1 Tf 12 0 0 12 36 700 Tm <0194>Tj ET Q"

        self.assertEqual(
            _decode_label_mcid_text(body, font_code_maps=_page_font_code_maps(page)),
            "●",
        )

    def test_unnamed_blank_square_code_still_reads_as_the_box(self) -> None:
        # With no font naming 0191, the code is the only evidence there is.
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Resources"] = pikepdf.Dictionary(
            {"/Font": pikepdf.Dictionary({"/C2_1": _type0_font(pdf, {"0194": "25CF"})})}
        )
        body = b"q BT /C2_1 1 Tf 12 0 0 12 36 700 Tm <0191>Tj ET Q"

        raw = _decode_label_mcid_text(body, font_code_maps=_page_font_code_maps(page))
        self.assertEqual(raw, "□")
        self.assertEqual(_spoken_list_label_text(raw, normalized_only=True), "blank")

    def test_unreadable_label_glyphs_are_not_spoken_as_blank(self) -> None:
        # No /ToUnicode at all: the label paints something nobody can read,
        # which is not the same as painting nothing.
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        body = b"q BT /C2_1 1 Tf 12 0 0 12 36 700 Tm <0194>Tj ET Q"

        raw = _decode_label_mcid_text(body, font_code_maps=_page_font_code_maps(page))
        self.assertIsNone(raw)
        self.assertIsNone(_spoken_list_label_text(raw, normalized_only=True))

    def test_symbolic_font_label_is_not_stamped_or_counted(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b'q /Lbl<</MCID 5 >> BDC ("#)Tj EMC '
            b"q /LBody<</MCID 6 >> BDC (Human somatic cells are diploid.) Tj EMC Q"
        )
        lbl = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Lbl"),
                "/K": 5,
                "/Pg": page.obj,
            }
        )
        lbody = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LBody"),
                "/K": 6,
                "/Pg": page.obj,
            }
        )
        li = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LI"),
                "/Pg": page.obj,
                "/K": pikepdf.Array([lbl, lbody]),
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([li]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)
        pdf_bytes = buf.getvalue()

        self.assertEqual(count_li_lbl_missing_actualtext(pdf_bytes), 0)
        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertNotIn("Lbl MCID 5", "\n".join(result.actions))
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertFalse(_mcid_bdc_has_actualtext(data, 5))
            li_elem = opened.Root["/StructTreeRoot"]["/K"][0]
            lbl_elem = li_elem["/K"][0]
            self.assertIsNone(lbl_elem.get("/ActualText"))

    def test_skip_list_item_label_actualtext_when_li_has_alt(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"q /Lbl<</MCID 5 >> BDC (1\\)   )Tj EMC "
            b"q /LBody<</MCID 6 >> BDC (Step text) Tj EMC Q"
        )
        lbl = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Lbl"),
                "/K": 5,
                "/Pg": page.obj,
            }
        )
        lbody = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LBody"),
                "/K": 6,
                "/Pg": page.obj,
            }
        )
        li = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LI"),
                "/Alt": "Step 1. Example list item",
                "/Pg": page.obj,
                "/K": pikepdf.Array([lbl, lbody]),
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([li]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)
        pdf_bytes = buf.getvalue()

        self.assertEqual(count_li_lbl_missing_actualtext(pdf_bytes), 0)
        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertNotIn("Lbl MCID 5", "\n".join(result.actions))
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertFalse(_mcid_bdc_has_actualtext(data, 5))

    def test_count_li_lbl_missing_actualtext_ignores_li_with_alt(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        lbl = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Lbl"),
                "/K": 5,
                "/Pg": page.obj,
            }
        )
        li = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LI"),
                "/Alt": "Step 1. Example",
                "/Pg": page.obj,
                "/K": pikepdf.Array([lbl]),
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([li]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)
        self.assertEqual(count_li_lbl_missing_actualtext(buf.getvalue()), 0)

    def test_repair_list_item_label_resolves_lbl_page_not_li_page(self) -> None:
        pdf = pikepdf.Pdf.new()
        page0 = pdf.add_blank_page()
        page1 = pdf.add_blank_page()
        page0["/Contents"] = pdf.make_stream(
            b"q /StyleSpan<</MCID 4 >> BDC (H) Tj EMC "
            b"q /StyleSpan<</MCID 6 >> BDC (P) Tj EMC"
        )
        page1["/Contents"] = pdf.make_stream(
            b"q /Lbl<</MCID 4 >> BDC (a\\)   )Tj EMC "
            b"q /LBody<</MCID 5 >> BDC (Answer) Tj EMC Q"
        )
        lbl = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Lbl"),
                "/K": 4,
                "/Pg": page1.obj,
            }
        )
        lbody = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LBody"),
                "/K": 5,
                "/Pg": page1.obj,
            }
        )
        li = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/LI"),
                "/Pg": page0.obj,
                "/K": pikepdf.Array([lbl, lbody]),
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([li]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)
        pdf_bytes = buf.getvalue()

        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertGreater(result.mcids_updated, 0)
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data_page0 = _read_page_contents(opened.pages[0]["/Contents"])
            data_page1 = _read_page_contents(opened.pages[1]["/Contents"])
            self.assertFalse(_mcid_bdc_has_actualtext(data_page0, 4))
            self.assertTrue(_mcid_bdc_has_actualtext(data_page1, 4))
            lbl = opened.Root["/StructTreeRoot"]["/K"][0]["/K"][0]
            self.assertEqual(str(lbl.get("/ActualText")), "option a")


_ENCODING_FIX_DIR = _REPO_ROOT / "tmp/encoding-fix-review"


def _li_alt_with_lbl_actualtext_violations(pdf_bytes: bytes) -> list[str]:
    """Struct paths where /LI has /Alt and a child /Lbl has /ActualText."""
    violations: list[str] = []
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            return violations

        def walk(obj: pikepdf.Dictionary, path: str) -> None:
            if obj.get("/S") == "/LI" and obj.get("/Alt") is not None:
                for child in obj.get("/K", []):
                    if not isinstance(child, pikepdf.Dictionary):
                        continue
                    if child.get("/S") == "/Lbl" and child.get("/ActualText") is not None:
                        violations.append(
                            f"{path}/Lbl has /ActualText under /LI with /Alt"
                        )
            kids = obj.get("/K")
            if isinstance(kids, pikepdf.Array):
                for index, kid in enumerate(kids):
                    if isinstance(kid, pikepdf.Dictionary):
                        walk(kid, f"{path}[{index}]")
            elif isinstance(kids, pikepdf.Dictionary):
                walk(kids, path)

        walk(struct_root, "StructTreeRoot")
    return violations


class BiosignalingNestedAltRegressionTests(unittest.TestCase):
    @unittest.skipUnless(
        (_ENCODING_FIX_DIR / "6f84ffb1-before.pdf").is_file(),
        "6f84ffb1 validation PDF not available",
    )
    def test_marked_sweep_avoids_lbl_actualtext_under_li_with_alt(self) -> None:
        pdf_bytes = (_ENCODING_FIX_DIR / "6f84ffb1-before.pdf").read_bytes()
        repaired, _ = repair_marked_content_actualtext(pdf_bytes)
        violations = _li_alt_with_lbl_actualtext_violations(repaired)
        self.assertEqual(
            violations,
            [],
            f"nested alt regression: {violations}",
        )


class PdfLiteralStringEncodingTests(unittest.TestCase):
    def test_astral_character_does_not_overflow_octal_escape(self) -> None:
        # U+1D436 ('𝐶', mathematical italic capital C) is common in Word
        # equation-editor formulas. Its codepoint's octal form is 6 digits;
        # a PDF \ddd escape is only ever 3, so encoding it that way corrupts
        # the string ("\352066" parses back as "ê066", not "𝐶").
        encoded = _pdf_literal_string("𝐶")
        self.assertEqual(encoded, b"<FEFF" + "𝐶".encode("utf-16-be").hex().upper().encode() + b">")
        self.assertEqual(_decode_pdf_actualtext_value(encoded), "𝐶")

    def test_plain_ascii_still_uses_literal_form(self) -> None:
        self.assertEqual(_pdf_literal_string("A+B"), b"(A+B)")

    def test_mixed_astral_and_ascii_round_trips(self) -> None:
        text = "=𝐶+𝐼+𝐺+𝑁"
        encoded = _pdf_literal_string(text)
        self.assertTrue(encoded.startswith(b"<FEFF"))
        self.assertEqual(_decode_pdf_actualtext_value(encoded), text)

    def test_injected_astral_actualtext_round_trips_through_content(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(b"q /P<</MCID 1 >> BDC (x) Tj EMC Q")
        buf = io.BytesIO()
        pdf.save(buf)

        with pikepdf.open(io.BytesIO(buf.getvalue())) as opened:
            changed = _inject_actualtext_on_page(
                opened,
                opened.pages[0],
                mcid=1,
                actual_text="𝐶",
            )
            self.assertTrue(changed)
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertEqual(_read_actualtext_from_mcid(data, 1), "𝐶")
            # No lone byte-0xEA-plus-digits corruption from an overflowed
            # octal escape.
            self.assertNotIn(b"\xea", data)


class OrphanMarkedContentTests(unittest.TestCase):
    def test_repair_orphan_table_and_span_diagram_labels(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"q /Table<</MCID 10 >> BDC /Im0 Do EMC "
            b"q /Span<</MCID 11 >> BDC (Input) Tj EMC "
            b"q /Table<</MCID 12 >> BDC 0 0 m 10 0 l S EMC "
            b"q /P<</MCID 1 >> BDC (body) Tj EMC"
        )
        body = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/P"),
                "/K": 1,
                "/Pg": page.obj,
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([body]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)
        pdf_bytes = buf.getvalue()

        self.assertGreater(count_orphan_marked_missing_actualtext(pdf_bytes), 0)
        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertGreater(result.mcids_updated, 0)
        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 0)

        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertTrue(_mcid_bdc_has_actualtext(data, 10))
            self.assertTrue(_mcid_bdc_has_actualtext(data, 11))
            # The paths-only orphan Table is now true artifact content: its
            # dead MCID is dropped so tagged-content audits stop flagging it.
            self.assertIsNone(_get_mcid_block(data, 12))
            self.assertIn(b"/Artifact BMC", data)

    def test_orphan_span_hex_strings_decode_through_tounicode(self) -> None:
        # ee112303: orphan Spans painted with a CID font (hex strings) were
        # skipped because the decoder could not read them. The font's
        # ToUnicode CMap can, tracked across Tf inside and before the block.
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        cmap = pdf.make_stream(
            b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            b"3 beginbfchar <0029> <0046> <004C> <0069> <0051> <006E> endbfchar\n"
            b"1 beginbfrange <0047> <0048> <0064> endbfrange"
        )
        font = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/Type0"),
                "/BaseFont": pikepdf.Name("/Calibri"),
                "/Encoding": pikepdf.Name("/Identity-H"),
                "/ToUnicode": cmap,
            }
        )
        page["/Resources"] = pikepdf.Dictionary(
            {"/Font": pikepdf.Dictionary({"/C2_0": font, "/T1_0": pikepdf.Dictionary({})})}
        )
        page["/Contents"] = pdf.make_stream(
            b"BT /C2_0 1 Tf q /T1_0 1 Tf Q "
            b"/Span<</MCID 5 >> BDC [<0029>12.7 <004C>0.5 <00510047>]TJ EMC ET "
            b"BT /Span<</MCID 6 >> BDC /T1_0 1 Tf <0029> Tj EMC ET "
            b"BT /P<</MCID 1 >> BDC (body) Tj EMC ET"
        )
        body = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/P"),
                "/K": 1,
                "/Pg": page.obj,
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([body]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)

        repaired, result = repair_marked_content_actualtext(buf.getvalue())

        self.assertIn(
            "orphan MCID 5: injected /ActualText 'Find' on Span", result.actions
        )
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertTrue(_mcid_bdc_has_actualtext(data, 5))
            # A hex string under a font with no readable CMap stays unspoken.
            self.assertFalse(_mcid_bdc_has_actualtext(data, 6))

    def test_orphan_decorative_span_becomes_artifact(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"q /Span<</MCID 5 >> BDC 0.5 0.8 0.3 scn 10 10 100 50 re f "
            b"0 0 m 10 0 l S EMC "
            b"q /Span<</MCID 6 >> BDC 10 10 100 50 re S BT (Keywords:) Tj ET EMC "
            b"q /P<</MCID 1 >> BDC (body) Tj EMC"
        )
        body = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/P"),
                "/K": 1,
                "/Pg": page.obj,
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([body]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)

        repaired, result = repair_marked_content_actualtext(buf.getvalue())

        artifact_actions = [
            action
            for action in result.actions
            if "retagged decorative Span to /Artifact" in action
        ]
        self.assertEqual(artifact_actions, [
            "orphan MCID 5: retagged decorative Span to /Artifact",
        ])
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            # Paths-only span became artifact content without an MCID.
            self.assertIsNone(_get_mcid_block(data, 5))
            self.assertIn(b"/Artifact BMC", data)
            # The span that also draws text keeps its marked content.
            block = _get_mcid_block(data, 6)
            self.assertIsNotNone(block)
            self.assertEqual(block[0], "Span")

    def test_orphan_span_showing_only_a_space_is_not_spoken_blank(self) -> None:
        # An empty decode is the answer box for a /Lbl, not for a Span that
        # shows one space next to a heading: "blank" there adds a word the
        # page never paints.
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"q /Span<</MCID 5 >> BDC 0 0 612 792 re W* n "
            b"BT /TT0 1 Tf 12 0 0 12 204 708 Tm ( )Tj ET EMC "
            b"q /P<</MCID 1 >> BDC (TOPIC: FACTORING POLYNOMIALS) Tj EMC"
        )
        body = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/P"),
                "/K": 1,
                "/Pg": page.obj,
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([body]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)

        repaired, result = repair_marked_content_actualtext(buf.getvalue())

        self.assertNotIn(
            "orphan MCID 5: injected /ActualText 'blank' on Span", result.actions
        )
        self.assertIn(
            "orphan MCID 5: retagged decorative Span to /Artifact", result.actions
        )
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertIsNone(_get_mcid_block(data, 5))
            self.assertNotIn(b"blank", data)
        # Artifacting it keeps the orphan out of the Other-elements count.
        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 0)


    def test_orphan_table_cell_fills_become_artifact(self) -> None:
        # Word draws table borders and cell shading as `re f*` fills in
        # orphan /Table blocks; the paths-only test looked for `m` alone.
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"q /Table<</MCID 5 >> BDC 0.557 0.667 0.859 rg "
            b"463.18 652.78 111.5 27.48 re f* EMC "
            b"q /Table<</MCID 6 >> BDC 10 10 100 50 re f* BT (Mass) Tj ET EMC "
            b"q /P<</MCID 1 >> BDC (body) Tj EMC"
        )
        body = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/P"),
                "/K": 1,
                "/Pg": page.obj,
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([body]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)

        repaired, result = repair_marked_content_actualtext(buf.getvalue())

        self.assertIn(
            "orphan MCID 5: retagged decorative Table to /Artifact", result.actions
        )
        self.assertIn(
            "orphan MCID 6: injected /ActualText 'Mass' on Table", result.actions
        )
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertIsNone(_get_mcid_block(data, 5))
            self.assertIn(b"/Artifact BMC", data)
            # The fill itself is still painted.
            self.assertIn(b"463.18 652.78 111.5 27.48 re f*", data)
        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 0)

    def test_orphan_table_showing_only_a_space_becomes_artifact(self) -> None:
        # Word pads an empty table cell with one space in its own /Table
        # block. It paints nothing, so it is neither spoken nor "blank".
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"q /Table<</MCID 5 >> BDC 465.19 600.1 109.46 58.44 re W* n "
            b"BT 0 g /TT0 1 Tf 9.96 0 0 9.96 520.3 646.54 Tm ( )Tj ET EMC "
            b"q /P<</MCID 1 >> BDC (body) Tj EMC"
        )
        body = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/P"),
                "/K": 1,
                "/Pg": page.obj,
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([body]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)

        repaired, result = repair_marked_content_actualtext(buf.getvalue())

        self.assertIn(
            "orphan MCID 5: retagged decorative Table to /Artifact", result.actions
        )
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertIsNone(_get_mcid_block(data, 5))
            self.assertNotIn(b"blank", data)
        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 0)


class OrphanFigureTests(unittest.TestCase):
    """Adobe Auto-Tag leaves /Figure marked content with a null ParentTree
    slot and no owning element; the sweep must artifact repeats the page
    already draws as decoration and surface unique graphics."""

    @staticmethod
    def _build(stream: bytes, *, owned_mcids_page2: list[int] | None = None) -> bytes:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(stream)
        form = pdf.make_stream(b"/Im0 Do")
        form["/Type"] = pikepdf.Name("/XObject")
        form["/Subtype"] = pikepdf.Name("/Form")
        page["/Resources"] = pikepdf.Dictionary(
            {"/XObject": pikepdf.Dictionary({"/Fm0": form})}
        )
        kids = [
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/P"),
                    "/K": 1,
                    "/Pg": page.obj,
                }
            )
        ]
        if owned_mcids_page2:
            page2 = pdf.add_blank_page()
            page2["/Contents"] = pdf.make_stream(
                b" ".join(
                    b"/P<</MCID %d >> BDC (x) Tj EMC" % m
                    for m in owned_mcids_page2
                )
            )
            kids.append(
                pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name("/P"),
                        "/K": pikepdf.Array(owned_mcids_page2),
                        "/Pg": page2.obj,
                    }
                )
            )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array(kids),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()

    def test_orphan_figure_repeat_of_artifact_draw_becomes_artifact(self) -> None:
        pdf_bytes = self._build(
            b"q /Figure<</MCID 5 >> BDC q /Fm0 Do Q EMC "
            b"/Artifact BMC q /Fm0 Do Q EMC "
            b"q /P<</MCID 1 >> BDC (body) Tj EMC"
        )
        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertIn(
            "orphan MCID 5: retagged decorative Figure to /Artifact",
            result.actions,
        )
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertIsNone(_get_mcid_block(data, 5))

    def test_orphan_figure_with_unique_graphic_gets_actualtext(self) -> None:
        pdf_bytes = self._build(
            b"q /Figure<</MCID 5 >> BDC q /Fm0 Do Q EMC "
            b"q /P<</MCID 1 >> BDC (body) Tj EMC"
        )
        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertIn(
            "orphan MCID 5: injected /ActualText 'Figure' on Figure",
            result.actions,
        )
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            if isinstance(data, str):
                data = data.encode("latin-1")
            block = re.search(
                rb"/Figure\s*<<[^>]*?/MCID\s+5(?!\d)[^>]*?>>", data
            )
            self.assertIsNotNone(block)
            self.assertIn(b"/ActualText (Figure)", block.group(0))

    def test_orphan_figure_showing_only_a_space_becomes_artifact(self) -> None:
        # A fraction bar plus Word's padding space shows no text; only a
        # non-whitespace decode makes an orphan Figure text-bearing.
        pdf_bytes = self._build(
            b"q /Figure<</MCID 5 >> BDC 498.7 625.06 10.26 0.66 re f* "
            b"BT /TT4 1 Tf 10.02 0 0 10.02 508.96 622.54 Tm ( )Tj ET EMC "
            b"q /P<</MCID 1 >> BDC (body) Tj EMC"
        )
        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertIn(
            "orphan MCID 5: retagged decorative Figure to /Artifact",
            result.actions,
        )
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertIsNone(_get_mcid_block(data, 5))
            self.assertIn(b"498.7 625.06 10.26 0.66 re f*", data)
        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 0)

    def test_orphan_figure_with_text_is_left_alone(self) -> None:
        pdf_bytes = self._build(
            b"q /Figure<</MCID 5 >> BDC q /Fm0 Do Q BT (label) Tj ET EMC "
            b"/Artifact BMC q /Fm0 Do Q EMC "
            b"q /P<</MCID 1 >> BDC (body) Tj EMC"
        )
        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertEqual(
            [a for a in result.actions if "orphan MCID 5" in a], []
        )
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            block = _get_mcid_block(data, 5)
            self.assertIsNotNone(block)
            self.assertEqual(block[0], "Figure")

    def test_orphan_detection_is_page_scoped(self) -> None:
        # Another page's struct tree owns MCID 5; page 1's Figure MCID 5 is
        # still an orphan and must be repaired.
        pdf_bytes = self._build(
            b"q /Figure<</MCID 5 >> BDC q /Fm0 Do Q EMC "
            b"/Artifact BMC q /Fm0 Do Q EMC "
            b"q /P<</MCID 1 >> BDC (body) Tj EMC",
            owned_mcids_page2=[5],
        )
        repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertIn(
            "orphan MCID 5: retagged decorative Figure to /Artifact",
            result.actions,
        )


class OrphanSpanFullExtentTests(unittest.TestCase):
    """Block-level /ActualText must speak every glyph the block shows."""

    @staticmethod
    def _build(stream: bytes) -> bytes:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(stream)
        body = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/P"),
                "/K": 1,
                "/Pg": page.obj,
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([body]),
            }
        )
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()

    def test_spoken_text_covers_shows_past_the_first_et(self) -> None:
        # Mirrors sociology f72a442e MCID 36: a heading in one BT..ET, then
        # more labels (some via TJ arrays) before the span's EMC. The stamped
        # /ActualText must include all of them or AT loses the labels.
        repaired, result = repair_marked_content_actualtext(
            self._build(
                b"q /Span<</MCID 7 >> BDC "
                b"BT [ (Example 2: South K) 9.6 (or) 6.8 (ea ) ] TJ ET "
                b"0 0 m 10 0 l S "
                b"BT (Male ) Tj (F) Tj (emale ) Tj "
                b"[ (Bir) -24.4 (th r) 9.3 (ate ) ] TJ ET EMC "
                b"q /P<</MCID 1 >> BDC (body) Tj EMC"
            )
        )
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertTrue(_mcid_bdc_has_actualtext(data, 7))
        injected = [a for a in result.actions if "orphan MCID 7" in a]
        self.assertEqual(len(injected), 1)
        self.assertIn("Example 2: South Korea Male Female Birth rate", injected[0])

    def test_undecodable_hex_show_blocks_injection(self) -> None:
        repaired, result = repair_marked_content_actualtext(
            self._build(
                b"q /Span<</MCID 7 >> BDC "
                b"BT (Legend: ) Tj <002a004100420043> Tj ET EMC "
                b"q /P<</MCID 1 >> BDC (body) Tj EMC"
            )
        )
        with pikepdf.open(io.BytesIO(repaired)) as opened:
            data = _read_page_contents(opened.pages[0]["/Contents"])
            self.assertFalse(_mcid_bdc_has_actualtext(data, 7))
        self.assertEqual([a for a in result.actions if "orphan MCID 7" in a], [])


class StructPageRefTests(unittest.TestCase):
    def test_set_struct_page_if_missing_accepts_page_dictionary(self) -> None:
        with pikepdf.new() as pdf:
            page = pdf.add_blank_page()
            struct = pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/Figure"),
                }
            )
            _set_struct_page_if_missing(struct, page.obj)
            self.assertEqual(struct["/Pg"], page.obj)


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


def _build_untagged_image_pdf(page_stream: bytes, *, struct_role: str = "/P") -> bytes:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    image = pikepdf.Stream(pdf, b"\xff")
    image["/Type"] = pikepdf.Name("/XObject")
    image["/Subtype"] = pikepdf.Name("/Image")
    image["/Width"] = 1
    image["/Height"] = 1
    image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
    image["/BitsPerComponent"] = 8
    page["/Resources"] = pikepdf.Dictionary(
        {"/XObject": pikepdf.Dictionary({"/Im1": image})}
    )
    page["/Contents"] = pdf.make_stream(page_stream)

    element = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name(struct_role),
            "/K": pikepdf.Array([0]),
            "/Pg": page.obj,
        }
    )
    document = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Document"),
            "/K": pikepdf.Array([element]),
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


class OcrTextReliabilityTests(unittest.TestCase):
    def test_chemical_structure_garble_is_rejected(self) -> None:
        from lib.marked_content_actualtext_sweep import _ocr_text_is_reliable

        self.assertFalse(_ocr_text_is_reliable("sterols =, R Be - SS HO 'S"))

    def test_stylized_infographic_garble_is_rejected(self) -> None:
        from lib.marked_content_actualtext_sweep import _ocr_text_is_reliable

        self.assertFalse(
            _ocr_text_is_reliable(
                "Sie) r-soluble Oo io) amins |oO absorbed Oye NOT stored "
                "Oj Excess is in urine"
            )
        )

    def test_running_text_is_reliable(self) -> None:
        from lib.marked_content_actualtext_sweep import _ocr_text_is_reliable

        self.assertTrue(
            _ocr_text_is_reliable(
                "Water-soluble vitamins are absorbed directly and not stored"
            )
        )

    def test_math_labels_with_variables_are_reliable(self) -> None:
        from lib.marked_content_actualtext_sweep import _ocr_text_is_reliable

        self.assertTrue(_ocr_text_is_reliable("x equals one"))
        self.assertTrue(_ocr_text_is_reliable("x1 equals 87"))

    def test_empty_and_wordless_text_is_unreliable(self) -> None:
        from lib.marked_content_actualtext_sweep import _ocr_text_is_reliable

        self.assertFalse(_ocr_text_is_reliable(""))
        self.assertFalse(_ocr_text_is_reliable("= - | %"))

    def test_stray_single_letters_are_rejected(self) -> None:
        from lib.marked_content_actualtext_sweep import _ocr_text_is_reliable

        self.assertFalse(_ocr_text_is_reliable('~ SURVEY "agou faa, r'))

    def test_articles_and_pronoun_i_do_not_count_as_junk(self) -> None:
        from lib.marked_content_actualtext_sweep import _ocr_text_is_reliable

        self.assertTrue(_ocr_text_is_reliable("I saw a chart of results"))


class UntaggedImageActualTextTests(unittest.TestCase):
    _IMAGE_ONLY_STREAM = (
        b"/P <</MCID 0>> BDC q 50 0 0 50 20 20 cm /Im1 Do Q EMC"
    )
    _MIXED_STREAM = (
        b"/P <</MCID 0>> BDC BT (Hello there) Tj ET "
        b"q 50 0 0 50 20 100 cm /Im1 Do Q EMC"
    )

    def test_repair_injects_actualtext_for_image_only_p_mcid(self) -> None:
        pdf_bytes = _build_untagged_image_pdf(self._IMAGE_ONLY_STREAM)
        with unittest.mock.patch(
            "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
            return_value="x equals one",
        ):
            repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertGreaterEqual(result.mcids_updated, 1)
        contents = _page_contents_text(repaired)
        self.assertEqual(_actualtext_for_mcid(contents, 0), "x equals one")

    def test_tiny_image_placement_skips_ocr(self) -> None:
        # 8x3pt placement: a tick mark, not a readable image — OCR of it
        # produces fake words, so the repair must use the generic label.
        pdf_bytes = _build_untagged_image_pdf(
            b"/P <</MCID 0>> BDC q 8 0 0 3 20 20 cm /Im1 Do Q EMC"
        )
        with unittest.mock.patch(
            "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
            return_value="Lele",
        ) as ocr:
            repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertGreaterEqual(result.mcids_updated, 1)
        ocr.assert_not_called()
        contents = _page_contents_text(repaired)
        self.assertEqual(_actualtext_for_mcid(contents, 0), "Figure")

    def test_repair_wraps_image_inside_mixed_text_mcid(self) -> None:
        pdf_bytes = _build_untagged_image_pdf(self._MIXED_STREAM)
        with unittest.mock.patch(
            "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
            return_value="y axis label",
        ):
            repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertGreaterEqual(result.mcids_updated, 1)
        contents = _page_contents_text(repaired)
        self.assertIn("(Hello there) Tj", contents)
        self.assertIsNone(_actualtext_for_mcid(contents, 0))
        self.assertRegex(
            contents,
            r"/Span << /ActualText \(y axis label\) >> BDC /Im1 Do EMC",
        )

    def test_repair_wraps_image_when_text_shown_via_tj_array(self) -> None:
        # Text painted with a TJ array (not plain Tj) must still block the
        # whole-span /ActualText stamp, or the real text is hidden from AT.
        pdf_bytes = _build_untagged_image_pdf(
            b"/Span <</MCID 0>> BDC BT [ (i) 0.5 (on per year. ) ] TJ ET "
            b"q 50 0 0 50 20 100 cm /Im1 Do Q EMC"
        )
        with unittest.mock.patch(
            "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
            return_value="",
        ):
            repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertGreaterEqual(result.mcids_updated, 1)
        contents = _page_contents_text(repaired)
        self.assertIn("[ (i) 0.5 (on per year. ) ] TJ", contents)
        self.assertIsNone(_actualtext_for_mcid(contents, 0))
        self.assertRegex(
            contents,
            r"/Span << /ActualText \(Figure\) >> BDC /Im1 Do EMC",
        )

    def test_repair_wraps_image_when_text_shown_via_hex_string(self) -> None:
        pdf_bytes = _build_untagged_image_pdf(
            b"/Span <</MCID 0>> BDC BT <1f> Tj ET "
            b"q 50 0 0 50 20 100 cm /Im1 Do Q EMC"
        )
        with unittest.mock.patch(
            "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
            return_value="",
        ):
            repaired, _result = repair_marked_content_actualtext(pdf_bytes)
        contents = _page_contents_text(repaired)
        self.assertIn("<1f> Tj", contents)
        self.assertIsNone(_actualtext_for_mcid(contents, 0))

    def test_whitespace_only_strings_still_count_as_image_only(self) -> None:
        pdf_bytes = _build_untagged_image_pdf(
            b"/P <</MCID 0>> BDC BT ( ) Tj [ ( ) ] TJ ET "
            b"q 50 0 0 50 20 20 cm /Im1 Do Q EMC"
        )
        with unittest.mock.patch(
            "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
            return_value="x equals one",
        ):
            repaired, _result = repair_marked_content_actualtext(pdf_bytes)
        contents = _page_contents_text(repaired)
        self.assertEqual(_actualtext_for_mcid(contents, 0), "x equals one")

    def test_repair_is_idempotent(self) -> None:
        for stream in (self._IMAGE_ONLY_STREAM, self._MIXED_STREAM):
            pdf_bytes = _build_untagged_image_pdf(stream)
            with unittest.mock.patch(
                "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
                return_value="stable text",
            ):
                repaired, first = repair_marked_content_actualtext(pdf_bytes)
                again, second = repair_marked_content_actualtext(repaired)
            self.assertGreaterEqual(first.mcids_updated, 1)
            untagged_actions = [
                action for action in second.actions if "untagged image" in action
            ]
            self.assertEqual(untagged_actions, [])
            self.assertEqual(
                _page_contents_text(again).count("stable text"),
                _page_contents_text(repaired).count("stable text"),
            )

    def test_repair_skips_element_with_alt(self) -> None:
        pdf_bytes = _build_untagged_image_pdf(self._IMAGE_ONLY_STREAM)
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            document = pdf.Root["/StructTreeRoot"]["/K"][0]
            element = document["/K"][0]
            element["/Alt"] = pikepdf.String("Already described")
            buf = io.BytesIO()
            pdf.save(buf)
            pdf_bytes = buf.getvalue()
        with unittest.mock.patch(
            "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
            return_value="unused",
        ):
            repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertIsNone(_actualtext_for_mcid(_page_contents_text(repaired), 0))
        untagged_actions = [
            action for action in result.actions if "untagged image" in action
        ]
        self.assertEqual(untagged_actions, [])

    def test_repair_covers_link_element_image_content(self) -> None:
        pdf_bytes = _build_untagged_image_pdf(
            b"/Link <</MCID 0>> BDC q 50 0 0 50 20 20 cm /Im1 Do Q EMC",
            struct_role="/Link",
        )
        with unittest.mock.patch(
            "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
            return_value="x1 equals 87",
        ):
            repaired, result = repair_marked_content_actualtext(pdf_bytes)
        self.assertGreaterEqual(result.mcids_updated, 1)
        self.assertEqual(
            _actualtext_for_mcid(_page_contents_text(repaired), 0),
            "x1 equals 87",
        )

    def test_repair_covers_orphan_image_mcid(self) -> None:
        pdf_bytes = _build_untagged_image_pdf(
            b"/P <</MCID 0>> BDC BT (Intro) Tj ET EMC "
            b"/P <</MCID 7>> BDC q 50 0 0 50 20 20 cm /Im1 Do Q EMC"
        )
        with unittest.mock.patch(
            "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
            return_value="orphan banner",
        ):
            repaired, result = repair_marked_content_actualtext(pdf_bytes)
        contents = _page_contents_text(repaired)
        self.assertEqual(_actualtext_for_mcid(contents, 7), "orphan banner")
        orphan_actions = [
            action for action in result.actions if "orphan image MCID 7" in action
        ]
        self.assertEqual(len(orphan_actions), 1)

    def test_orphan_phase_skips_struct_owned_mcid(self) -> None:
        pdf = pikepdf.open(io.BytesIO(_build_untagged_image_pdf(self._IMAGE_ONLY_STREAM)))
        element = pdf.Root["/StructTreeRoot"]["/K"][0]["/K"][0]
        element["/Alt"] = pikepdf.String("Covered")
        buf = io.BytesIO()
        pdf.save(buf)
        with unittest.mock.patch(
            "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
            return_value="unused",
        ):
            repaired, result = repair_marked_content_actualtext(buf.getvalue())
        self.assertIsNone(_actualtext_for_mcid(_page_contents_text(repaired), 0))
        untagged_actions = [
            action for action in result.actions if "untagged image" in action
        ]
        self.assertEqual(untagged_actions, [])

    def test_list_flags_orphan_image_mcid(self) -> None:
        pdf_bytes = _build_untagged_image_pdf(
            b"/P <</MCID 0>> BDC BT (Intro) Tj ET EMC "
            b"/P <</MCID 7>> BDC q 50 0 0 50 20 20 cm /Im1 Do Q EMC"
        )
        self.assertIn(
            "page1 mcid7 (orphan)",
            list_untagged_image_mcids_missing_actualtext(pdf_bytes),
        )

    def test_list_untagged_image_mcids_before_and_after_repair(self) -> None:
        for stream in (self._IMAGE_ONLY_STREAM, self._MIXED_STREAM):
            pdf_bytes = _build_untagged_image_pdf(stream)
            self.assertEqual(
                list_untagged_image_mcids_missing_actualtext(pdf_bytes),
                ["page1 mcid0"],
            )
            with unittest.mock.patch(
                "lib.marked_content_actualtext_sweep._ocr_page_clip_text",
                return_value="spoken text",
            ):
                repaired, _ = repair_marked_content_actualtext(pdf_bytes)
            self.assertEqual(
                list_untagged_image_mcids_missing_actualtext(repaired), []
            )


def _build_contentless_span_pdf() -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page.Contents = pdf.make_stream(
        b"/Span <</MCID 0>> BDC BT /F1 12 Tf 10 100 Td (x) Tj ET EMC\n"
    )

    def elem(name: str, **extra: object) -> pikepdf.Dictionary:
        data = {"/Type": pikepdf.Name("/StructElem"), "/S": pikepdf.Name(f"/{name}")}
        for key, value in extra.items():
            data[f"/{key}"] = value
        return pikepdf.Dictionary(data)

    spoken = elem("Span", ActualText=pikepdf.String("x"), K=pikepdf.Array([0]), Pg=page.obj)
    empty = elem("Span", ActualText=pikepdf.String(" "), K=pikepdf.Array([]))
    paragraph = elem("P", K=pikepdf.Array([spoken, empty]))
    empty_figure = pdf.make_indirect(elem("Figure", Alt=pikepdf.String("Table")))
    document = elem("Document", K=pikepdf.Array([paragraph, empty_figure]))
    pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
        {"/Type": pikepdf.Name("/StructTreeRoot"), "/K": pikepdf.Array([document])}
    )
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


class ContentlessAltTests(unittest.TestCase):
    def test_strips_actualtext_from_empty_span_but_keeps_spoken_span(self) -> None:
        repaired, result = repair_marked_content_actualtext(_build_contentless_span_pdf())

        pdf = pikepdf.open(io.BytesIO(repaired))
        spoken, empty = pdf.Root.StructTreeRoot.K[0].K[0].K
        self.assertEqual(str(spoken.ActualText), "x")
        self.assertNotIn("/ActualText", empty)
        self.assertIn(
            "removed ActualText from empty Span with no page content", result.actions
        )

    def test_removes_empty_figure_whose_alt_names_no_content(self) -> None:
        first, _ = repair_marked_content_actualtext(_build_contentless_span_pdf())
        second, _ = repair_marked_content_actualtext(first)

        with pikepdf.open(io.BytesIO(first)) as pdf:
            document = pdf.Root.StructTreeRoot.K[0]
            self.assertEqual([str(kid.S) for kid in document.K], ["/P"])
        self.assertEqual(second, first)


def _build_grouping_alt_over_nested_alt_pdf() -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page.Contents = pdf.make_stream(
        b"/Lbl <</MCID 0>> BDC BT /F1 12 Tf 10 150 Td (c\\)) Tj ET EMC\n"
        b"/LBody <</MCID 1>> BDC BT /F1 12 Tf 30 150 Td (Salting out) Tj ET EMC\n"
        b"/Span <</MCID 2>> BDC BT /F1 12 Tf 10 100 Td (a) Tj ET EMC\n"
        b"/Lbl <</MCID 3>> BDC BT /F1 12 Tf 10 50 Td (d\\)) Tj ET EMC\n"
        b"/LBody <</MCID 4>> BDC BT /F1 12 Tf 30 50 Td (Dialysis) Tj ET EMC\n"
    )
    elem = _struct_elem
    labelled_item = elem(
        "LI",
        Alt=pikepdf.String("List item 17"),
        K=pikepdf.Array(
            [
                elem("Lbl", ActualText=pikepdf.String("option c"), K=pikepdf.Array([0]), Pg=page.obj),
                elem("LBody", K=pikepdf.Array([1]), Pg=page.obj),
            ]
        ),
    )
    plain_item = elem(
        "LI",
        Alt=pikepdf.String("List item 18"),
        K=pikepdf.Array(
            [
                elem("Lbl", K=pikepdf.Array([3]), Pg=page.obj),
                elem("LBody", K=pikepdf.Array([4]), Pg=page.obj),
            ]
        ),
    )
    paragraph = elem(
        "P",
        ActualText=pikepdf.String("tasting sugar. mannose loses its sweet"),
        K=pikepdf.Array([elem("Span", ActualText=pikepdf.String("\u03b1"), K=pikepdf.Array([2]), Pg=page.obj)]),
    )
    document = elem(
        "Document",
        K=pikepdf.Array([elem("L", K=pikepdf.Array([labelled_item, plain_item])), paragraph]),
    )
    pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
        {"/Type": pikepdf.Name("/StructTreeRoot"), "/K": pikepdf.Array([document])}
    )
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
    return _save(pdf)


class GroupingAltOverNestedAltTests(unittest.TestCase):
    """Word's container alt over a labelled child fails "Nested alternate text"."""

    def test_lists_struct_alt_under_struct_alt(self) -> None:
        self.assertEqual(
            list_nested_alternate_text(_build_grouping_alt_over_nested_alt_pdf()),
            [
                "/LI 'List item 17' > /Lbl 'option c'",
                "/P 'tasting sugar. mannose loses its sweet' > /Span '\u03b1'",
            ],
        )

    def test_drops_container_alt_and_keeps_the_nested_text(self) -> None:
        repaired, result = repair_marked_content_actualtext(
            _build_grouping_alt_over_nested_alt_pdf()
        )

        self.assertEqual(list_nested_alternate_text(repaired), [])
        pdf = pikepdf.open(io.BytesIO(repaired))
        items, paragraph = pdf.Root.StructTreeRoot.K[0].K
        labelled_item, plain_item = items.K
        self.assertNotIn("/Alt", labelled_item)
        self.assertEqual(str(labelled_item.K[0].ActualText), "option c")
        self.assertEqual(str(plain_item.Alt), "List item 18")
        self.assertNotIn("/ActualText", paragraph)
        self.assertEqual(str(paragraph.K[0].ActualText), "\u03b1")
        self.assertIn(
            "removed /Alt from /LI whose descendants carry their own alternate text",
            result.actions,
        )
        self.assertIn(
            "removed /ActualText from /P whose descendants carry their own alternate text",
            result.actions,
        )

        second, second_result = repair_marked_content_actualtext(repaired)
        self.assertEqual(second_result.actions, [])
        self.assertEqual(second, repaired)


def _struct_elem(name: str, **extra: object) -> pikepdf.Dictionary:
    data = {"/Type": pikepdf.Name("/StructElem"), "/S": pikepdf.Name(f"/{name}")}
    for key, value in extra.items():
        data[f"/{key}"] = value
    return pikepdf.Dictionary(data)


def _save(pdf: pikepdf.Pdf) -> bytes:
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


def _parent_tree(*entries: tuple[int, list[object]]) -> pikepdf.Dictionary:
    nums: list[object] = []
    for key, owners in entries:
        nums.extend([key, pikepdf.Array(owners)])
    return pikepdf.Dictionary({"/Nums": pikepdf.Array(nums)})


class UnnamedGlyphTests(unittest.TestCase):
    """A ToUnicode destination that names no character is not spoken text."""

    def test_private_use_destination_is_unmapped(self) -> None:
        # Word maps Cambria Math's stretchy parentheses to U+F000-U+F003.
        self.assertIsNone(_tounicode_text("F000"))
        self.assertIsNone(_tounicode_text("0041F003"))
        self.assertEqual(_tounicode_text("0041"), "A")

    def test_encoding_stage_placeholder_is_unmapped(self) -> None:
        # The characterEncoding stage renames U+F000 to a Box Drawing
        # character after this sweep ran; the second pass must not speak it.
        self.assertIsNone(_tounicode_text("2501"))

    def test_injection_refuses_private_use_text(self) -> None:
        data = b"/Span<</MCID 3 >> BDC BT <1234> Tj ET EMC"
        repaired, changed = _inject_actualtext_in_data(
            data, mcid=3, actual_text="\uf000x\uf001"
        )
        self.assertFalse(changed)
        self.assertEqual(repaired, data)

    def test_private_use_code_is_named_from_the_glyph_outline(self) -> None:
        # business-statistics 2026-09-15: Word maps the Cambria Math glyphs
        # its equation layout substitutes to U+F000..; the embedded subset
        # keeps their outlines at the full font's ids. Code 0D46 (minus) is
        # named from its outline, code 0003 (no outline) reads as a space,
        # and the CMap's own "0042" for code 0041 is never overridden.
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        program = _truetype_program({MINUS_GID: MINUS_RECORD})
        page["/Resources"] = pikepdf.Dictionary(
            {
                "/Font": pikepdf.Dictionary(
                    {
                        "/C2_0": _type0_font(
                            pdf, {"0D46": "F000", "0041": "0042"}, program
                        ),
                        "/C2_1": _type0_font(pdf, {"0D46": "F000"}),
                    }
                )
            }
        )
        maps = _page_font_code_maps(page)
        self.assertEqual(
            _decode_shown_text_in_order(
                b"BT /C2_0 1 Tf <0D4600030041> Tj ET", font_code_maps=maps
            ),
            "\u2212 B",
        )
        # Without a program there is no evidence, and the block stays unread.
        self.assertIsNone(
            _decode_shown_text_in_order(
                b"BT /C2_1 1 Tf <0D46> Tj ET", font_code_maps=maps
            )
        )

    def test_orphan_span_of_private_use_glyphs_gets_actualtext(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        program = _truetype_program({MINUS_GID: MINUS_RECORD})
        page["/Resources"] = pikepdf.Dictionary(
            {
                "/Font": pikepdf.Dictionary(
                    {"/C2_0": _type0_font(pdf, {"0D46": "F000", "0035": "0035"}, program)}
                )
            }
        )
        page["/Contents"] = pdf.make_stream(
            b"BT /C2_0 1 Tf /Span<</MCID 5 >> BDC <0D460035> Tj EMC ET "
            b"BT /P<</MCID 1 >> BDC (body) Tj EMC ET"
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([_struct_elem("P", K=1, Pg=page.obj)]),
            }
        )
        repaired, result = repair_marked_content_actualtext(_save(pdf))
        self.assertEqual(
            _read_actualtext_from_mcid(_page_contents_text(repaired).encode("latin1"), 5),
            "\u22125",
        )
        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 0)

    def test_block_opening_with_q_paints_with_the_restored_font(self) -> None:
        # a753fab4 p3 MCID 57: the block starts with `Q Q Q`, so its text
        # shows in the font the outer state saved, not the one in effect at
        # the BDC. The q-stack travels with the font.
        data = b"BT /C2_0 1 Tf q /T1_0 1 Tf q /T1_1 1 Tf /Span<</MCID 5 >> BDC Q Q <0041> Tj EMC"
        state = _font_in_effect_at(data, data.index(b"/Span"))
        self.assertEqual(state, FontState("T1_1", ("C2_0", "T1_0")))
        maps = {"C2_0": ({b"\x00\x41": "A"}, (2,))}
        body = b"Q Q <0041> Tj"
        self.assertEqual(
            _decode_shown_text_in_order(body, font_code_maps=maps, initial_font=state),
            "A",
        )
        self.assertIsNone(
            _decode_shown_text_in_order(body, font_code_maps=maps, initial_font="T1_1")
        )


class PagelessAltContentTests(unittest.TestCase):
    """A /Figure /Alt "Table" with bare MCIDs and no /Pg up its chain gets the
    page whose ParentTree names it for those MCIDs."""

    @staticmethod
    def _build(*, block: bytes) -> bytes:
        pdf = pikepdf.Pdf.new()
        page0 = pdf.add_blank_page()
        page1 = pdf.add_blank_page()
        page0["/Contents"] = pdf.make_stream(b"/P<</MCID 0 >> BDC (body) Tj EMC")
        page1["/Contents"] = pdf.make_stream(b"/Figure<</MCID 3 >> BDC " + block + b" EMC")
        page0["/StructParents"] = 0
        page1["/StructParents"] = 1
        paragraph = pdf.make_indirect(_struct_elem("P", K=pikepdf.Array([0]), Pg=page0.obj))
        figure = pdf.make_indirect(
            _struct_elem("Figure", Alt=pikepdf.String("Table"), K=pikepdf.Array([3]))
        )
        document = pdf.make_indirect(
            _struct_elem("Document", K=pikepdf.Array([paragraph, figure]))
        )
        paragraph["/P"] = document
        figure["/P"] = document
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([document]),
                "/ParentTree": _parent_tree(
                    (0, [paragraph]), (1, [None, None, None, figure])
                ),
            }
        )
        return _save(pdf)

    def test_pageless_alt_figure_gets_the_page_its_parent_tree_entry_names(self) -> None:
        repaired, result = repair_marked_content_actualtext(
            self._build(block=b"q /Im0 Do Q")
        )

        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            figure = pdf.Root["/StructTreeRoot"]["/K"][0]["/K"][1]
            self.assertEqual(figure["/Pg"].objgen, pdf.pages[1].obj.objgen)
        self.assertIn(
            "set /Pg on pageless Figure with alternate text to page 2 named by "
            "its ParentTree entries for MCIDs [3]",
            result.actions,
        )

    def test_placeholder_table_alt_is_not_stamped_over_shown_text(self) -> None:
        repaired, _result = repair_marked_content_actualtext(
            self._build(block=b"BT /F1 10 Tf (2 m/s) Tj ET")
        )

        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            data = _read_page_contents(pdf.pages[1]["/Contents"])
        self.assertFalse(_mcid_bdc_has_actualtext(data, 3))


class DeadAltFigureOwnerTests(unittest.TestCase):
    """An unreachable /Figure with /Alt that the ParentTree names for orphan
    Figure blocks goes back under the nearest reachable container."""

    @staticmethod
    def _build(*, alt: str, bfchars: dict[str, str] | None = None) -> bytes:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        first = b"<0001> Tj" if bfchars is not None else b"(x) Tj"
        page["/Contents"] = pdf.make_stream(
            b"/Figure<</MCID 0 >> BDC BT /F1 10 Tf " + first + b" ET EMC "
            b"/Figure<</MCID 1 >> BDC BT /F1 10 Tf (= y) Tj ET EMC "
            b"/P<</MCID 2 >> BDC (body) Tj EMC"
        )
        if bfchars is not None:
            page["/Resources"] = pikepdf.Dictionary(
                {"/Font": pikepdf.Dictionary({"/F1": _type0_font(pdf, bfchars)})}
            )
        page["/StructParents"] = 0
        paragraph = pdf.make_indirect(_struct_elem("P", K=pikepdf.Array([2]), Pg=page.obj))
        # The earlier round left this Figure over the table it reverted; it
        # speaks its own alt, so nothing may be hung under it, and it owns no
        # content, so the sweep drops it.
        reverted = pdf.make_indirect(
            _struct_elem("Figure", Alt=pikepdf.String("Table"), K=pikepdf.Array([]), Pg=page.obj)
        )
        document = pdf.make_indirect(
            _struct_elem("Document", K=pikepdf.Array([reverted, paragraph]))
        )
        paragraph["/P"] = document
        reverted["/P"] = document
        # The abandoned cell subtree: TR -> TD -> equation Figure, none reachable.
        row = pdf.make_indirect(_struct_elem("TR", P=reverted))
        cell = pdf.make_indirect(_struct_elem("TD", P=row))
        equation = pdf.make_indirect(
            _struct_elem(
                "Figure",
                Alt=pikepdf.String(alt),
                C=pikepdf.Array([pikepdf.Name("/fb-region-inlineFormula")]),
                K=pikepdf.Array([0, 1]),
                Pg=page.obj,
                P=cell,
            )
        )
        cell["/K"] = pikepdf.Array([equation])
        row["/K"] = pikepdf.Array([cell])
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([document]),
                "/ParentTree": _parent_tree((0, [equation, equation, paragraph])),
            }
        )
        return _save(pdf)

    def test_dead_figure_is_reattached_after_the_element_it_sat_under(self) -> None:
        original = self._build(alt="x equals y")
        self.assertEqual(count_orphan_marked_missing_actualtext(original), 2)

        repaired, result = repair_marked_content_actualtext(original)

        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 0)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            document = pdf.Root["/StructTreeRoot"]["/K"][0]
            kids = document["/K"]
            # The walk then retags the inline-formula Figure as a Span.
            self.assertEqual([str(kid["/S"]) for kid in kids], ["/Span", "/P"])
            self.assertEqual(str(kids[0]["/Alt"]), "x equals y")
            self.assertEqual(kids[0]["/P"].objgen, document.objgen)
        self.assertIn(
            "page 1: reattached unreachable Figure 'x equals y' owning orphan "
            "MCIDs [0, 1] under Document",
            result.actions,
        )

    def test_caption_only_alt_is_dropped_when_the_blocks_name_their_glyphs(self) -> None:
        # Reattached with this alt, the equation would be spoken as a caption;
        # without it the element becomes a Span and the glyphs speak.
        original = self._build(
            alt="Figure 10. Spoken formula notation for inline chemistry text."
        )

        repaired, result = repair_marked_content_actualtext(original)

        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 0)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            document = pdf.Root["/StructTreeRoot"]["/K"][0]
            kids = document["/K"]
            self.assertEqual([str(kid["/S"]) for kid in kids], ["/Span", "/P"])
            self.assertNotIn("/Alt", kids[0])
            self.assertNotIn("/C", kids[0])
            data = _read_page_contents(pdf.pages[0]["/Contents"])
        self.assertEqual(_get_mcid_block(data, 0)[0], "Span")
        self.assertFalse(_mcid_bdc_has_actualtext(data, 0))
        self.assertIn(
            "page 1: reattached unreachable Figure 'Figure 10. Spoken formula "
            "notation for i' owning orphan MCIDs [0, 1] under Document, dropping "
            "the alt that names none of its text",
            result.actions,
        )

    def test_caption_only_alt_over_an_unnamed_glyph_stays_out_of_the_tree(self) -> None:
        # The first block's glyph maps to Private Use, so nothing in the file
        # says what the reader would hear; the Figure stays for review.
        original = self._build(
            alt="Figure 10. Spoken formula notation for inline chemistry text.",
            bfchars={"0001": "F000"},
        )

        repaired, result = repair_marked_content_actualtext(original)

        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 2)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            document = pdf.Root["/StructTreeRoot"]["/K"][0]
            self.assertEqual(len(document["/K"]), 1)
            data = _read_page_contents(pdf.pages[0]["/Contents"])
        self.assertFalse(_mcid_bdc_has_actualtext(data, 0))
        self.assertFalse(any("reattached" in action for action in result.actions))


class ParentTreeNamedFigureContentTests(unittest.TestCase):
    """An orphan text block drawn inside a reachable Figure's layout /BBox,
    whose ParentTree entry names that Figure, is linked into its /K."""

    def test_orphan_text_inside_the_figure_bbox_is_linked(self) -> None:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"/Figure<</MCID 0 >> BDC q /Im0 Do Q EMC "
            b"/Span<</MCID 1 >> BDC BT /F1 10 Tf 1 0 0 1 50 60 Tm (A = B) Tj ET EMC "
            b"/Span<</MCID 2 >> BDC BT /F1 10 Tf 1 0 0 1 300 60 Tm (caption) Tj ET EMC"
        )
        page["/StructParents"] = 0
        figure = pdf.make_indirect(
            _struct_elem(
                "Figure",
                Alt=pikepdf.String("Equation A equals B."),
                K=pikepdf.Array([0]),
                Pg=page.obj,
                A=pikepdf.Dictionary(
                    {"/O": pikepdf.Name("/Layout"), "/BBox": pikepdf.Array([40, 40, 120, 100])}
                ),
            )
        )
        document = pdf.make_indirect(_struct_elem("Document", K=pikepdf.Array([figure])))
        figure["/P"] = document
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([document]),
                "/ParentTree": _parent_tree((0, [figure, figure, figure])),
            }
        )

        repaired, result = repair_marked_content_actualtext(_save(pdf))

        with pikepdf.open(io.BytesIO(repaired)) as opened:
            linked = opened.Root["/StructTreeRoot"]["/K"][0]["/K"][0]
            self.assertEqual([int(kid) for kid in linked["/K"]], [0, 1])
        self.assertIn(
            "page 1: linked orphan MCID 1 into the Figure 'Equation A equals B.' "
            "its ParentTree entry names",
            result.actions,
        )
        self.assertFalse(any("MCID 2 into" in action for action in result.actions))


_LEGACY_INTENDED_TEXT = "Find v\u0302 equals 4\u00ee plus 3\u0135."
_LEGACY_OVERFLOWING_LITERAL = b"(Find v\\1402 equals 4\\356 plus 3\\465.)"
_LEGACY_LONG_RUN_TEXT = "\U0001d466 \u2212 4"
_LEGACY_LONG_RUN_LITERAL = b"(\\352146 \\21022 4)"


def _build_legacy_octal_actualtext_pdf(
    *, owner_alt: str | None, literal: bytes = _LEGACY_OVERFLOWING_LITERAL
) -> bytes:
    """A Figure block whose ActualText the pre-2026-09-02 writer spelled in octal."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    page["/Contents"] = pdf.make_stream(
        b"/Figure << /MCID 0 /ActualText " + literal + b" >> BDC "
        b"BT /F1 12 Tf 10 100 Td (v) Tj ET EMC "
        b"/Span << /MCID 1 /ActualText (a \\\\465 literal backslash) >> BDC "
        b"BT /F1 12 Tf 10 80 Td (b) Tj ET EMC"
    )
    page["/StructParents"] = 0
    entries = {
        "/Type": pikepdf.Name("/StructElem"),
        "/S": pikepdf.Name("/Span"),
        "/Pg": page.obj,
        "/K": 0,
    }
    if owner_alt is not None:
        entries["/Alt"] = pikepdf.String(owner_alt)
    owner = pdf.make_indirect(pikepdf.Dictionary(entries))
    other = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Span"),
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
                "/K": pikepdf.Array([owner, other]),
            }
        )
    )
    owner["/P"] = document
    other["/P"] = document
    root = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([document]),
                "/ParentTree": pdf.make_indirect(
                    pikepdf.Dictionary(
                        {"/Nums": pikepdf.Array([0, pikepdf.Array([owner, other])])}
                    )
                ),
            }
        )
    )
    document["/P"] = root
    pdf.Root["/StructTreeRoot"] = root
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _page_content(pdf_bytes: bytes) -> bytes:
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        return _read_page_contents(pdf.pages[0].obj["/Contents"])


class OverflowingOctalEscapeRepairTests(unittest.TestCase):
    def test_owning_element_text_that_re_encodes_to_the_literal_is_used(self) -> None:
        repaired, result = repair_marked_content_actualtext(
            _build_legacy_octal_actualtext_pdf(owner_alt=_LEGACY_INTENDED_TEXT)
        )

        content = _page_content(repaired)
        self.assertIn(
            b"/MCID 0 /ActualText " + _pdf_literal_string(_LEGACY_INTENDED_TEXT),
            content,
        )
        self.assertNotIn(b"\\465", content.replace(b"\\\\465", b""))
        self.assertTrue(
            any(
                "rewrote /ActualText" in action and "owning element's Alt" in action
                for action in result.actions
            ),
            result.actions,
        )

    def test_full_digit_run_is_read_when_no_element_text_matches(self) -> None:
        repaired, result = repair_marked_content_actualtext(
            _build_legacy_octal_actualtext_pdf(owner_alt=None)
        )

        self.assertIn(
            b"/MCID 0 /ActualText " + _pdf_literal_string(_LEGACY_INTENDED_TEXT),
            _page_content(repaired),
        )
        self.assertTrue(
            any("escape's full digit run" in action for action in result.actions),
            result.actions,
        )

    def test_escaped_backslash_before_digits_is_not_an_overflow(self) -> None:
        repaired, _result = repair_marked_content_actualtext(
            _build_legacy_octal_actualtext_pdf(owner_alt=None)
        )

        self.assertIn(
            b"/MCID 1 /ActualText (a \\\\465 literal backslash)", _page_content(repaired)
        )

    def test_long_digit_run_is_rewritten_when_the_owning_element_confirms_it(self) -> None:
        repaired, result = repair_marked_content_actualtext(
            _build_legacy_octal_actualtext_pdf(
                owner_alt=_LEGACY_LONG_RUN_TEXT, literal=_LEGACY_LONG_RUN_LITERAL
            )
        )

        self.assertIn(
            b"/MCID 0 /ActualText " + _pdf_literal_string(_LEGACY_LONG_RUN_TEXT),
            _page_content(repaired),
        )
        self.assertTrue(
            any("owning element's Alt" in action for action in result.actions),
            result.actions,
        )

    def test_long_digit_run_is_rewritten_when_the_owning_element_contains_it(self) -> None:
        repaired, result = repair_marked_content_actualtext(
            _build_legacy_octal_actualtext_pdf(
                owner_alt=f"Recall {_LEGACY_LONG_RUN_TEXT} is a line.",
                literal=_LEGACY_LONG_RUN_LITERAL,
            )
        )

        self.assertIn(
            b"/MCID 0 /ActualText " + _pdf_literal_string(_LEGACY_LONG_RUN_TEXT),
            _page_content(repaired),
        )
        self.assertTrue(
            any("found in the owning element's Alt" in action for action in result.actions),
            result.actions,
        )

    def test_long_digit_run_alone_is_left_as_written(self) -> None:
        repaired, result = repair_marked_content_actualtext(
            _build_legacy_octal_actualtext_pdf(
                owner_alt=None, literal=_LEGACY_LONG_RUN_LITERAL
            )
        )

        self.assertIn(
            b"/MCID 0 /ActualText " + _LEGACY_LONG_RUN_LITERAL, _page_content(repaired)
        )
        self.assertFalse(
            any("rewrote /ActualText" in action for action in result.actions),
            result.actions,
        )

    def test_rewritten_literal_is_left_alone_on_the_next_run(self) -> None:
        repaired, _first = repair_marked_content_actualtext(
            _build_legacy_octal_actualtext_pdf(owner_alt=None)
        )
        again, second = repair_marked_content_actualtext(repaired)

        self.assertFalse(
            any("rewrote /ActualText" in action for action in second.actions),
            second.actions,
        )
        self.assertEqual(_page_content(again), _page_content(repaired))


if __name__ == "__main__":
    unittest.main()


class ClippedAwayFigureTextTests(unittest.TestCase):
    """Word draws a text box that crosses a page break on both pages, clipped
    to each page's share. The far side's orphan /Figure block shows text that
    no pixel of the page carries, so it is decoration, not held text."""

    @staticmethod
    def _build(stream: bytes) -> bytes:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(stream)
        page["/StructParents"] = 0
        paragraph = pdf.make_indirect(_struct_elem("P", K=pikepdf.Array([1]), Pg=page.obj))
        document = pdf.make_indirect(_struct_elem("Document", K=pikepdf.Array([paragraph])))
        paragraph["/P"] = document
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([document]),
                "/ParentTree": _parent_tree((0, [None, paragraph])),
            }
        )
        return _save(pdf)

    def test_text_outside_the_clip_set_before_the_block_is_an_artifact(self) -> None:
        # The clip is set two levels up and the block opens with the Q that
        # pops back to it, as in business-statistics 4338f8a9 page 2.
        original = self._build(
            b"q 0.24 0 0 0.24 0 0 cm 0 0 500 500 re W n q "
            b"/Figure<</MCID 0 >> BDC Q BT /F1 10 Tf 1 0 0 1 300 700 Tm (far side) Tj ET q EMC "
            b"Q Q /P<</MCID 1 >> BDC BT /F1 10 Tf 1 0 0 1 20 20 Tm (body) Tj ET EMC"
        )
        self.assertEqual(count_orphan_marked_missing_actualtext(original), 1)

        repaired, result = repair_marked_content_actualtext(original)

        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 0)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            data = _read_page_contents(pdf.pages[0]["/Contents"])
            self.assertEqual(len(pdf.Root["/StructTreeRoot"]["/K"][0]["/K"]), 1)
        self.assertIn(b"/Artifact BMC Q BT /F1 10 Tf 1 0 0 1 300 700 Tm (far side) Tj", data)
        self.assertIn(
            "orphan MCID 0: retagged Figure whose text the page clips away to /Artifact",
            result.actions,
        )

    def test_text_inside_the_clip_stays_held(self) -> None:
        # Same clip, glyphs inside it, and a hex string no font names: the
        # block paints and cannot be spoken, so nothing may claim it.
        original = self._build(
            b"q 0.24 0 0 0.24 0 0 cm 0 0 500 500 re W n q "
            b"/Figure<</MCID 0 >> BDC Q BT /F1 10 Tf 1 0 0 1 20 50 Tm <0041> Tj ET q EMC "
            b"Q Q /P<</MCID 1 >> BDC BT /F1 10 Tf 1 0 0 1 20 20 Tm (body) Tj ET EMC"
        )

        repaired, result = repair_marked_content_actualtext(original)

        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 1)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            data = _read_page_contents(pdf.pages[0]["/Contents"])
        self.assertIn(b"/Figure<</MCID 0 >> BDC", data)
        self.assertFalse(_mcid_bdc_has_actualtext(data, 0))
        self.assertFalse(
            any("clips away" in action or "adopted" in action for action in result.actions)
        )


class UnownedTextFigureBlockTests(unittest.TestCase):
    """An orphan /Figure block showing text whose ParentTree entry is null has
    no owner anywhere in the file; it gets an element of its own, placed
    among its neighbours in content order and named in the ParentTree."""

    @staticmethod
    def _build(figure_body: bytes, *, bfchars: dict[str, str] | None = None) -> bytes:
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"/P<</MCID 0 >> BDC BT /F1 10 Tf 1 0 0 1 20 700 Tm (Heading) Tj ET EMC "
            b"/Figure<</MCID 1 >> BDC BT /F1 10 Tf 1 0 0 1 20 600 Tm " + figure_body + b" ET EMC "
            b"/P<</MCID 2 >> BDC BT /F1 10 Tf 1 0 0 1 20 500 Tm (body) Tj ET EMC"
        )
        if bfchars is not None:
            page["/Resources"] = pikepdf.Dictionary(
                {"/Font": pikepdf.Dictionary({"/F1": _type0_font(pdf, bfchars)})}
            )
        page["/StructParents"] = 0
        heading = pdf.make_indirect(_struct_elem("P", K=pikepdf.Array([0]), Pg=page.obj))
        body = pdf.make_indirect(_struct_elem("P", K=pikepdf.Array([2]), Pg=page.obj))
        document = pdf.make_indirect(_struct_elem("Document", K=pikepdf.Array([heading, body])))
        heading["/P"] = document
        body["/P"] = document
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([document]),
                "/ParentTree": _parent_tree((0, [heading, None, body])),
            }
        )
        return _save(pdf)

    def test_block_becomes_an_element_between_its_neighbours(self) -> None:
        original = self._build(b"(HYPOTHESIS TEST  Write Hypotheses) Tj")
        self.assertEqual(count_orphan_marked_missing_actualtext(original), 1)

        repaired, result = repair_marked_content_actualtext(original)

        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 0)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            page = pdf.pages[0]
            document = pdf.Root["/StructTreeRoot"]["/K"][0]
            kids = document["/K"]
            # The non-image Figure pass then makes it a Span speaking its glyphs.
            self.assertEqual([str(kid["/S"]) for kid in kids], ["/P", "/Span", "/P"])
            adopted = kids[1]
            self.assertEqual(int(adopted["/K"]), 1)
            self.assertEqual(adopted["/P"].objgen, document.objgen)
            self.assertEqual(adopted["/Pg"].objgen, page.obj.objgen)
            self.assertNotIn("/Alt", adopted)
            entry = pdf.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"][1]
            self.assertEqual(entry[1].objgen, adopted.objgen)
            data = _read_page_contents(page["/Contents"])
        self.assertEqual(_get_mcid_block(data, 1)[0], "Span")
        self.assertFalse(_mcid_bdc_has_actualtext(data, 1))
        self.assertIn(
            "page 1: adopted unowned Figure block MCID 1 showing "
            "'HYPOTHESIS TEST Write Hypotheses' as a Figure under Document "
            "and named it in the ParentTree",
            result.actions,
        )

        again, second = repair_marked_content_actualtext(repaired)
        self.assertEqual(count_orphan_marked_missing_actualtext(again), 0)
        self.assertFalse(any("adopted" in action for action in second.actions))

    def test_block_is_placed_by_the_next_neighbour_when_the_nearest_is_out_of_order(self) -> None:
        # An earlier stage appended the element for MCID 1 after the one for
        # MCID 2, so the slot beside it does not bracket MCID 0; the slot
        # beside MCID 2's element does.
        pdf = pikepdf.Pdf.new()
        page = pdf.add_blank_page()
        page["/Contents"] = pdf.make_stream(
            b"/Figure<</MCID 0 >> BDC BT /F1 10 Tf (TOPIC: SERIES) Tj ET EMC "
            b"/P<</MCID 1 >> BDC BT /F1 10 Tf (PRACTICE) Tj ET EMC "
            b"/P<</MCID 2 >> BDC BT /F1 10 Tf (body) Tj ET EMC"
        )
        page["/StructParents"] = 0
        practice = pdf.make_indirect(_struct_elem("P", K=pikepdf.Array([1]), Pg=page.obj))
        body = pdf.make_indirect(_struct_elem("P", K=pikepdf.Array([2]), Pg=page.obj))
        document = pdf.make_indirect(_struct_elem("Document", K=pikepdf.Array([body, practice])))
        practice["/P"] = document
        body["/P"] = document
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([document]),
                "/ParentTree": _parent_tree((0, [None, practice, body])),
            }
        )

        repaired, result = repair_marked_content_actualtext(_save(pdf))

        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 0)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            kids = pdf.Root["/StructTreeRoot"]["/K"][0]["/K"]
            # The adopted element is the Span; its neighbours keep their order.
            self.assertEqual([str(kid["/S"]) for kid in kids], ["/Span", "/P", "/P"])
            self.assertEqual(int(kids[0]["/K"]), 0)
            self.assertEqual([int(kid["/K"][0]) for kid in list(kids)[1:]], [2, 1])
        self.assertTrue(any("adopted unowned Figure block MCID 0" in a for a in result.actions))

    def test_block_naming_a_private_use_glyph_is_left_alone(self) -> None:
        # Cambria Math's stretchy delimiters map to U+F000..; an /Alt holding
        # one would speak nothing for it, so the block stays for review.
        original = self._build(
            b"<00010002> Tj", bfchars={"0001": "0041", "0002": "F000"}
        )

        repaired, result = repair_marked_content_actualtext(original)

        self.assertEqual(count_orphan_marked_missing_actualtext(repaired), 1)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(len(pdf.Root["/StructTreeRoot"]["/K"][0]["/K"]), 2)
            data = _read_page_contents(pdf.pages[0]["/Contents"])
        self.assertIn(b"/Figure<</MCID 1 >> BDC", data)
        self.assertFalse(_mcid_bdc_has_actualtext(data, 1))
        self.assertFalse(any("adopted" in action for action in result.actions))
