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

from lib.marked_content_actualtext_sweep import (
    _decode_pdf_actualtext_value,
    _get_mcid_block,
    _inject_actualtext_in_data,
    _inject_actualtext_on_page,
    _mcid_bdc_has_actualtext,
    _pdf_literal_string,
    _read_actualtext_from_mcid,
    _read_page_contents,
    _resolve_struct_page,
    _set_struct_page_if_missing,
    _spoken_list_label_text,
    count_li_lbl_missing_actualtext,
    count_orphan_marked_missing_actualtext,
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

        self.assertIn("removed /Alt from LI with nested ExtraCharSpan", actions)
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


if __name__ == "__main__":
    unittest.main()
