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
    _inject_actualtext_in_data,
    _inject_actualtext_on_page,
    _mcid_bdc_has_actualtext,
    _read_page_contents,
    _resolve_struct_page,
    count_li_lbl_missing_actualtext,
    count_orphan_marked_missing_actualtext,
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
        self.assertEqual(_page_contents_text(repaired_once), _page_contents_text(repaired_twice))


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
    def test_repair_nested_figure_mcid_and_duplicate_struct_alt(self) -> None:
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

        self.assertIn("dropped [11]", "\n".join(result.actions))
        self.assertIn("stripped duplicate /ActualText", "\n".join(result.actions))
        self.assertIn("/Span<< /MCID 11", contents)
        self.assertNotIn("/Figure<< /MCID 11", contents)
        self.assertNotIn("/Figure<< /MCID 10 /ActualText", contents)

        with pikepdf.open(io.BytesIO(repaired)) as opened:
            root = opened.Root["/StructTreeRoot"]
            figure_elem = root["/K"][0]["/K"][0]
            self.assertEqual(figure_elem.get("/S"), pikepdf.Name("/Figure"))
            self.assertEqual(list(figure_elem.get("/K", [])), [10])
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
            block = _get_mcid_block(data, 12)
            self.assertIsNotNone(block)
            self.assertEqual(block[0], "Artifact")


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
