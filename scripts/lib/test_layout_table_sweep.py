"""Tests for layout table header repair."""

from __future__ import annotations

import io
import unittest
from pathlib import Path

import pikepdf

from lib.layout_table_sweep import repair_layout_tables
from lib.pdf_a11y_audit import audit_pdf_bytes

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tmp" / "fin-acct-ch2"


def _make_two_row_table() -> bytes:
    pdf = pikepdf.new()
    struct_root = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array([]),
        }
    )
    pdf.Root["/StructTreeRoot"] = struct_root
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})

    header_cells = [
        pikepdf.Dictionary({"/Type": pikepdf.Name("/StructElem"), "/S": pikepdf.Name("/TD")}),
        pikepdf.Dictionary({"/Type": pikepdf.Name("/StructElem"), "/S": pikepdf.Name("/Span")}),
    ]
    data_cells = [
        pikepdf.Dictionary({"/Type": pikepdf.Name("/StructElem"), "/S": pikepdf.Name("/TD")})
        for _ in range(6)
    ]
    table = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Table"),
            "/K": pikepdf.Array(
                [
                    pikepdf.Dictionary(
                        {
                            "/Type": pikepdf.Name("/StructElem"),
                            "/S": pikepdf.Name("/TR"),
                            "/K": pikepdf.Array(header_cells),
                        }
                    ),
                    pikepdf.Dictionary(
                        {
                            "/Type": pikepdf.Name("/StructElem"),
                            "/S": pikepdf.Name("/TR"),
                            "/K": pikepdf.Array(data_cells),
                        }
                    ),
                ]
            ),
        }
    )
    struct_root["/K"] = pikepdf.Array([table])

    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


class LayoutTableSweepTests(unittest.TestCase):
    def test_two_row_header_table_gets_th_scope_and_headers(self) -> None:
        repaired, result = repair_layout_tables(_make_two_row_table())

        self.assertIn("annotated two-row header table", result.actions[0])
        audit = audit_pdf_bytes(repaired)
        self.assertEqual(audit.table_count, 1)
        self.assertEqual(audit.tables_without_th, 0)

        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            rows = table["/K"]
            header_cells = rows[0]["/K"]
            self.assertEqual(len(header_cells), 6)
            self.assertEqual(header_cells[0]["/S"], "/TH")
            self.assertEqual(header_cells[0]["/Scope"], "/Column")
            self.assertNotIn("/Attributes", header_cells[0])
            data_cells = rows[1]["/K"]
            self.assertEqual(len(data_cells), 6)
            self.assertEqual(data_cells[0]["/Headers"][0], "tbl1-h0")
            self.assertEqual(data_cells[3]["/Headers"][0], "tbl1-h3")

    def test_financial_accounting_ch2_previews_get_headers(self) -> None:
        if not FIXTURE_DIR.exists():
            self.skipTest("chapter 2 fixture PDFs not downloaded")

        for pdf_path in sorted(FIXTURE_DIR.glob("*.pdf")):
            repaired, _result = repair_layout_tables(pdf_path.read_bytes())
            audit = audit_pdf_bytes(repaired)
            with self.subTest(topic=pdf_path.stem):
                self.assertEqual(
                    audit.tables_without_th,
                    0,
                    f"{pdf_path.stem} still has tables without /TH",
                )


    def test_two_row_header_tables_have_matching_row_widths(self) -> None:
        if not FIXTURE_DIR.exists():
            self.skipTest("chapter 2 fixture PDFs not downloaded")

        for pdf_path in sorted(FIXTURE_DIR.glob("*.pdf")):
            repaired, _result = repair_layout_tables(pdf_path.read_bytes())
            with pikepdf.open(io.BytesIO(repaired)) as pdf:
                struct = pdf.Root.get("/StructTreeRoot")
                tables = [
                    node
                    for node in _iter_tables(struct)
                    if isinstance(node, pikepdf.Dictionary) and node.get("/S") == "/Table"
                ]
                for table in tables:
                    rows = [
                        node
                        for node in _iter_tables(table)
                        if isinstance(node, pikepdf.Dictionary) and node.get("/S") == "/TR"
                    ]
                    if len(rows) != 2:
                        continue
                    counts = [len(row.get("/K", [])) for row in rows]
                    with self.subTest(topic=pdf_path.stem, counts=counts):
                        self.assertEqual(counts[0], counts[1])


def _iter_tables(obj: pikepdf.Object):
    if isinstance(obj, pikepdf.Dictionary):
        yield obj
        kids = obj.get("/K")
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                yield from _iter_tables(kid)
        elif kids is not None:
            yield from _iter_tables(kids)
    elif isinstance(obj, pikepdf.Array):
        for kid in obj:
            yield from _iter_tables(kid)


if __name__ == "__main__":
    unittest.main()
