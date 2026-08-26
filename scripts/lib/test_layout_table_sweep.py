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


def _make_table_pdf(
    row_widths: list[list[int]],
    *,
    row_header_rows: tuple[int, ...] = (),
    header_rows: tuple[int, ...] = (),
    col_span_first_row: int | None = None,
    table_class: str | None = None,
    header_id: str | None = None,
    data_headers: list[str] | None = None,
    page_count: int = 1,
) -> bytes:
    pdf = pikepdf.new()
    pages = [pdf.add_blank_page(page_size=(200, 200)) for _ in range(page_count)]
    rows: list[pikepdf.Dictionary] = []
    for row_index, widths in enumerate(row_widths):
        cells: list[pikepdf.Dictionary] = []
        for cell_index, _width in enumerate(widths):
            cell = pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/TD"),
                    "/K": pikepdf.String(f"r{row_index}c{cell_index}"),
                    "/Pg": pages[0].obj,
                }
            )
            if row_index in row_header_rows and cell_index == 0:
                cell["/S"] = pikepdf.Name("/TH")
                cell["/Scope"] = pikepdf.Name("/Row")
            elif row_index in header_rows:
                cell["/S"] = pikepdf.Name("/TH")
                cell["/Scope"] = pikepdf.Name("/Column")
            if row_index == 0 and cell_index == 0 and header_id:
                cell["/ID"] = pikepdf.String(header_id)
            if row_index == 0 and cell_index == 0 and col_span_first_row:
                cell["/A"] = pikepdf.Dictionary(
                    {
                        "/O": pikepdf.Name("/Table"),
                        "/ColSpan": col_span_first_row,
                    }
                )
            if row_index == 1 and cell_index == 0 and data_headers:
                cell["/Headers"] = pikepdf.Array(
                    [pikepdf.String(value) for value in data_headers]
                )
            cells.append(cell)
        rows.append(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/TR"),
                    "/K": pikepdf.Array(cells),
                    "/Pg": pages[0].obj,
                }
            )
        )
    table = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Table"),
            "/K": pikepdf.Array(rows),
            "/Pg": pages[0].obj,
        }
    )
    if table_class:
        table["/C"] = pikepdf.Name(table_class)
    root = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array([table]),
        }
    )
    pdf.Root["/StructTreeRoot"] = root
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


def _make_adobe_layout_table() -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 400))
    rows: list[pikepdf.Dictionary] = []
    column_indices = (
        (0, 1, 2),
        (1, 2, 3),
        (0, 1, 4),
        (0, 1, 4),
        (1, 2, 3),
    )
    spans = ((1, 1, 1), (1, 1, 1), (1, 1, 1), (1, 3, 1), (1, 1, 1))
    empty_cells = {(1, 1), (3, 1), (4, 2)}
    for row_index, (indices, row_spans) in enumerate(zip(column_indices, spans)):
        cells: list[pikepdf.Dictionary] = []
        for cell_index, (column_index, col_span) in enumerate(zip(indices, row_spans)):
            content = pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/MCR"),
                    "/MCID": row_index * 3 + cell_index,
                    "/Pg": page.obj,
                }
            )
            cell = pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/TD"),
                    "/K": pikepdf.Array([] if (row_index, cell_index) in empty_cells else [content]),
                    "/Pg": page.obj,
                    "/ID": pikepdf.String(f"cell-{row_index}-{cell_index}"),
                    "/A": pikepdf.Array(
                        [
                            pikepdf.Dictionary(
                                {
                                    "/O": pikepdf.Name("/Table"),
                                    "/ADBE_ColIndex": column_index,
                                    "/ColSpan": col_span,
                                }
                            )
                        ]
                    ),
                }
            )
            pdf.make_indirect(content)
            pdf.make_indirect(cell)
            content["/P"] = cell
            if row_index == 3 and cell_index == 1:
                cell["/S"] = pikepdf.Name("/TH")
            cells.append(cell)
        row = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/TR"),
                "/K": pikepdf.Array(cells),
                "/Pg": page.obj,
                "/ID": pikepdf.String(f"row-{row_index}"),
            }
        )
        pdf.make_indirect(row)
        for cell in cells:
            cell["/P"] = row
        rows.append(row)
    table = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Table"),
            "/K": pikepdf.Array(rows),
            "/Pg": page.obj,
            "/ID": pikepdf.String("adobe-table"),
            "/A": pikepdf.Array(
                [
                    pikepdf.Dictionary(
                        {
                            "/O": pikepdf.Name("/Table"),
                            "/ADBE_NumCol": 5,
                            "/ADBE_NumRow": 5,
                        }
                    ),
                    pikepdf.Dictionary(
                        {
                            "/O": pikepdf.Name("/ADBE_Table"),
                            "/ADBE_TableProcess": pikepdf.Name("/Neptune"),
                        }
                    ),
                ]
            ),
        }
    )
    pdf.make_indirect(table)
    root = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array([table]),
        }
    )
    pdf.make_indirect(root)
    table["/P"] = root
    for row in rows:
        row["/P"] = table
    pdf.Root["/StructTreeRoot"] = root
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
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
            repaired, result = repair_layout_tables(pdf_path.read_bytes())
            audit = audit_pdf_bytes(repaired)
            with self.subTest(topic=pdf_path.stem):
                self.assertEqual(audit.tables_without_th, result.ambiguous_tables)
                self.assertEqual(audit.table_count, result.tables_found - result.unwrapped_grid)


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

    def test_simple_column_headers_get_ids_scopes_and_associations(self) -> None:
        repaired, result = repair_layout_tables(_make_table_pdf([[2, 2], [2, 2]]))

        self.assertEqual(result.conflicts, [])
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            headers = table["/K"][0]["/K"]
            self.assertEqual([cell["/S"] for cell in headers], ["/TH", "/TH"])
            self.assertEqual([cell["/Scope"] for cell in headers], ["/Column", "/Column"])
            self.assertEqual(len({str(cell["/ID"]) for cell in headers}), 2)
            data = table["/K"][1]["/K"]
            self.assertEqual(data[0]["/Headers"][0], headers[0]["/ID"])
            self.assertEqual(data[1]["/Headers"][0], headers[1]["/ID"])

    def test_row_headers_are_associated_without_changing_their_scope(self) -> None:
        repaired, _result = repair_layout_tables(
            _make_table_pdf(
                [[2, 2], [2, 2], [2, 2]],
                row_header_rows=(1, 2),
            )
        )

        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            row_header = table["/K"][1]["/K"][0]
            data_cell = table["/K"][1]["/K"][1]
            self.assertEqual(row_header["/S"], "/TH")
            self.assertEqual(row_header["/Scope"], "/Row")
            self.assertIn(str(row_header["/ID"]), [str(value) for value in data_cell["/Headers"]])

    def test_multirow_header_span_is_preserved_and_applies_to_columns(self) -> None:
        repaired, _result = repair_layout_tables(
            _make_table_pdf(
                [[1], [2, 2], [2, 2]],
                col_span_first_row=2,
                header_rows=(0, 1),
            )
        )

        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            header = table["/K"][0]["/K"][0]
            self.assertEqual(header["/S"], "/TH")
            self.assertEqual(header["/A"]["/ColSpan"], 2)
            header_id = str(header["/ID"])
            for cell in table["/K"][2]["/K"]:
                self.assertIn(header_id, [str(value) for value in cell["/Headers"]])

    def test_irregular_genuine_table_stays_a_table(self) -> None:
        repaired, result = repair_layout_tables(
            _make_table_pdf([[2, 2], [3, 3, 3], [2, 2]])
        )

        self.assertFalse(any("reverted" in action for action in result.actions))
        self.assertEqual(result.annotated_data_tables, 0)
        self.assertEqual(result.ambiguous_tables, 1)
        self.assertTrue(any("differing logical row widths" in item for item in result.unresolved))
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(
                pdf.Root["/StructTreeRoot"]["/K"][0]["/S"],
                "/Table",
            )

    def test_adobe_layout_table_is_unwrapped_without_losing_structure(self) -> None:
        original = _make_adobe_layout_table()
        with pikepdf.open(io.BytesIO(original)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            original_rows = list(table["/K"])
            self.assertEqual(len(original_rows), 5)
            self.assertEqual([len(row["/K"]) for row in original_rows], [3] * 5)
            self.assertEqual(
                [
                    sum(int(cell["/A"][0].get("/ColSpan", 1)) for cell in row["/K"])
                    for row in original_rows
                ],
                [3, 3, 3, 5, 3],
            )
            self.assertEqual(table["/A"][0]["/ADBE_NumCol"], 5)
            self.assertEqual(original_rows[3]["/K"][1]["/A"][0]["/O"], "/Table")
            self.assertEqual(original_rows[3]["/K"][1]["/A"][0]["/ColSpan"], 3)
            original_cells = [
                cell
                for row in original_rows
                for cell in row["/K"]
            ]
            original_content = [
                cell["/K"][0]
                for cell in original_cells
                if len(cell["/K"])
            ]
            original_objgens = {
                obj.objgen
                for obj in [table] + original_rows + original_cells + original_content
            }

        repaired, result = repair_layout_tables(original)

        self.assertEqual(result.unwrapped_grid, 1)
        self.assertTrue(any("Adobe auto-tagged layout" in action for action in result.actions))
        self.assertEqual(result.annotated_data_tables, 0)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            section = pdf.Root["/StructTreeRoot"]["/K"][0]
            rows = list(section["/K"])
            cells = [cell for row in rows for cell in row["/K"]]
            self.assertEqual(section["/S"], "/Sect")
            self.assertFalse(
                any(node.get("/S") == "/Table" for node in _iter_tables(section))
            )
            self.assertEqual([row["/S"] for row in rows], ["/Div"] * 5)
            self.assertEqual([cell["/S"] for cell in cells], ["/Span"] * 15)
            self.assertEqual(section["/ID"], "adobe-table")
            self.assertEqual(section["/P"].objgen, pdf.Root["/StructTreeRoot"].objgen)
            self.assertEqual(
                [row["/ID"] for row in rows],
                [f"row-{index}" for index in range(5)],
            )
            self.assertTrue(
                all(row["/P"].objgen == section.objgen for row in rows)
            )
            self.assertTrue(
                all(cell["/P"].objgen == row.objgen for row in rows for cell in row["/K"])
            )
            self.assertEqual(
                [cell["/ID"] for cell in cells],
                [
                    f"cell-{row_index}-{cell_index}"
                    for row_index in range(5)
                    for cell_index in range(3)
                ],
            )
            self.assertTrue(
                all(
                    cell["/K"][0]["/P"].objgen == cell.objgen
                    for cell in cells
                    if len(cell["/K"])
                )
            )
            self.assertEqual(
                {
                    obj.objgen
                    for obj in [section] + rows + cells
                    + [cell["/K"][0] for cell in cells if len(cell["/K"])]
                },
                original_objgens,
            )
            self.assertEqual(
                [
                    cell["/K"][0]["/MCID"]
                    for cell in cells
                    if len(cell["/K"])
                ],
                [0, 1, 2, 3, 5, 6, 7, 8, 9, 11, 12, 13],
            )
            self.assertEqual(
                [
                    len(cell["/K"])
                    for cell in cells
                ],
                [1, 1, 1, 1, 0, 1, 1, 1, 1, 1, 0, 1, 1, 1, 0],
            )

        repaired_again, second_result = repair_layout_tables(repaired)
        self.assertEqual(repaired_again, repaired)
        self.assertEqual(second_result.actions, [])

    def test_explicitly_marked_layout_table_is_unwrapped(self) -> None:
        repaired, result = repair_layout_tables(
            _make_table_pdf([[3, 3, 3]], table_class="/layout-table")
        )

        self.assertEqual(result.unwrapped_grid, 1)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(
                pdf.Root["/StructTreeRoot"]["/K"][0]["/S"],
                "/Sect",
            )

    def test_explicit_single_cell_layout_removes_table_cell_role(self) -> None:
        repaired, result = repair_layout_tables(
            _make_table_pdf([[1]], table_class="/layout-table")
        )

        self.assertEqual(result.unwrapped_1x1, 1)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            section = pdf.Root["/StructTreeRoot"]["/K"][0]
            row = section["/K"][0]
            self.assertEqual(section["/S"], "/Sect")
            self.assertEqual(row["/S"], "/Div")
            self.assertEqual(row["/K"][0]["/S"], "/Span")

    def test_existing_metadata_conflicts_are_preserved_and_reported(self) -> None:
        original = _make_table_pdf(
            [[2, 2], [2, 2]],
            header_id="keep-header",
            data_headers=["unknown-header"],
        )
        repaired, result = repair_layout_tables(original)

        self.assertTrue(result.conflicts)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            self.assertEqual(table["/K"][0]["/K"][0]["/ID"], "keep-header")
            headers = [str(value) for value in table["/K"][1]["/K"][0]["/Headers"]]
            self.assertIn("unknown-header", headers)

    def test_output_page_count_and_second_run_are_stable(self) -> None:
        original = _make_table_pdf([[2, 2], [2, 2]], page_count=2)
        repaired_once, _first = repair_layout_tables(original)
        repaired_twice, second = repair_layout_tables(repaired_once)

        self.assertEqual(repaired_twice, repaired_once)
        self.assertEqual(second.actions, [])
        with pikepdf.open(io.BytesIO(repaired_twice)) as pdf:
            self.assertEqual(len(pdf.pages), 2)


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
