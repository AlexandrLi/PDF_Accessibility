"""Tests for layout table header repair."""

from __future__ import annotations

import io
import unittest
from pathlib import Path

import pikepdf

from lib.layout_table_sweep import (
    _logical_row_widths,
    _owner_attribute_values,
    _table_rows_and_cells,
    repair_layout_tables,
)
from lib.pdf_a11y_audit import audit_pdf_bytes

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tmp" / "fin-acct-ch2"
MACRO_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "pdfs"
    / "accessibility-issue-map"
    / "macroeconomics"
    / "2026-08-26"
    / "originals"
    / "f8690451.pdf"
)
MICRO_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "pdfs"
    / "accessibility-issue-map"
    / "microeconomics"
    / "2026-08-28"
    / "originals"
)
COLLEGE_ALGEBRA_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "pdfs"
    / "accessibility-issue-map"
    / "college-algebra"
    / "2026-08-28"
    / "originals"
    / "56d27cf6.pdf"
)


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


def _make_neptune_placeholder_table(*, conflicting_header_index: bool = False) -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 400))
    real_headers = []
    for header_index, column_index in enumerate((0, 2)):
        header = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/TH"),
                "/Scope": pikepdf.Name("/Column"),
                "/ID": pikepdf.String(f"neptune-header-{header_index * 2}"),
                "/Headers": pikepdf.Array([pikepdf.String("existing-header")]),
                "/K": pikepdf.String(f"header-{header_index}"),
                "/Pg": page.obj,
                "/A": pikepdf.Array(
                    [
                        pikepdf.Dictionary(
                            {
                                "/O": pikepdf.Name("/Table"),
                                "/ColSpan": 2,
                                "/ADBE_ColIndex": (
                                    1 if conflicting_header_index and header_index == 1
                                    else column_index
                                ),
                            }
                        )
                    ]
                ),
            }
        )
        pdf.make_indirect(header)
        real_headers.append(header)

    placeholders = []
    for column_index in (1, 3):
        placeholder = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/TH"),
                "/Scope": pikepdf.Name("/Column"),
                "/ID": pikepdf.String(f"neptune-header-{column_index}"),
                "/K": pikepdf.Array([]),
                "/Pg": page.obj,
            }
        )
        pdf.make_indirect(placeholder)
        placeholders.append(placeholder)

    header_row = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/TR"),
            "/K": pikepdf.Array(
                [real_headers[0], placeholders[0], real_headers[1], placeholders[1]]
            ),
            "/Pg": page.obj,
        }
    )
    pdf.make_indirect(header_row)

    data_cells = []
    for cell_index in range(4):
        cell = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/TD"),
                "/K": pikepdf.String(f"data-{cell_index}"),
                "/Pg": page.obj,
                "/Headers": pikepdf.Array(
                [pikepdf.String(f"neptune-header-{cell_index}")]
                ),
            }
        )
        pdf.make_indirect(cell)
        data_cells.append(cell)
    data_row = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/TR"),
            "/K": pikepdf.Array(data_cells),
            "/Pg": page.obj,
        }
    )
    pdf.make_indirect(data_row)

    table = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Table"),
            "/K": pikepdf.Array([header_row, data_row]),
            "/Pg": page.obj,
            "/A": pikepdf.Array(
                [
                    pikepdf.Dictionary(
                        {
                            "/O": pikepdf.Name("/Table"),
                            "/ADBE_NumCol": 4,
                            "/ADBE_NumRow": 2,
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
    pdf.Root["/StructTreeRoot"] = root
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
    table["/P"] = root
    header_row["/P"] = table
    data_row["/P"] = table
    for cell in real_headers + placeholders:
        cell["/P"] = header_row
    for cell in data_cells:
        cell["/P"] = data_row

    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


def _make_word_border_table(
    row_widths: list[int],
    *,
    rich_layout: bool = False,
) -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 400))
    next_mcid = 0
    rows: list[pikepdf.Dictionary] = []
    for row_index, width in enumerate(row_widths):
        cells: list[pikepdf.Dictionary] = []
        for cell_index in range(width):
            mcid_count = (
                12
                if rich_layout
                else 1
                if row_index == 0 and cell_index == 0
                else 2
            )
            kids = pikepdf.Array(range(next_mcid, next_mcid + mcid_count))
            next_mcid += mcid_count
            cells.append(
                pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name("/TD"),
                        "/K": kids,
                        "/Pg": page.obj,
                    }
                )
            )
        border = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Span"),
                "/K": next_mcid,
                "/Pg": page.obj,
            }
        )
        next_mcid += 1
        rows.append(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/TR"),
                    "/K": pikepdf.Array([*cells, border]),
                    "/Pg": page.obj,
                }
            )
        )
    table = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Table"),
            "/K": pikepdf.Array(rows),
            "/Pg": page.obj,
        }
    )
    root = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array([table]),
        }
    )
    pdf.Root["/StructTreeRoot"] = root
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
    pdf.docinfo["/Producer"] = pikepdf.String("Microsoft® Word 2010")
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


def _make_pdf_lib_overlapping_table(*, valid_indices: bool = True) -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 400))
    roles = (("/TH", "/TH"), ("/TD", "/TD"), ("/TH", "/TD"))
    indices = ((0, 1), (1, 0), (0, 1 if valid_indices else 0))
    rows: list[pikepdf.Dictionary] = []
    mcid = 0
    for row_index, (row_roles, row_indices) in enumerate(zip(roles, indices)):
        cells: list[pikepdf.Dictionary] = []
        for role, column_index in zip(row_roles, row_indices):
            table_attributes = pikepdf.Dictionary(
                {
                    "/O": pikepdf.Name("/Table"),
                    "/ADBE_ColIndex": column_index,
                }
            )
            if row_index == 0:
                table_attributes["/RowSpan"] = 3
            cells.append(
                pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name(role),
                        "/K": mcid,
                        "/Pg": page.obj,
                        "/A": pikepdf.Array(
                            [
                                table_attributes,
                                pikepdf.Dictionary(
                                    {
                                        "/O": pikepdf.Name("/ADBE_Table"),
                                        "/ADBE_Confidence": 50,
                                    }
                                ),
                            ]
                        ),
                    }
                )
            )
            mcid += 1
        rows.append(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/TR"),
                    "/K": pikepdf.Array(cells),
                    "/Pg": page.obj,
                }
            )
        )
    table = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Table"),
            "/K": pikepdf.Array(rows),
            "/Pg": page.obj,
        }
    )
    pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array([table]),
        }
    )
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
    pdf.docinfo["/Producer"] = pikepdf.String(
        "pdf-lib (https://github.com/Hopding/pdf-lib)"
    )
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

    def test_singleton_dictionary_row_k_is_processed(self) -> None:
        original = _make_table_pdf([[1], [1]])
        output = io.BytesIO()
        with pikepdf.open(io.BytesIO(original)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            for row in table["/K"]:
                row["/K"] = row["/K"][0]
            pdf.save(output)

        repaired, result = repair_layout_tables(output.getvalue())

        self.assertEqual(result.annotated_data_tables, 1)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            header = table["/K"][0]["/K"]
            data = table["/K"][1]["/K"]
            self.assertEqual(header["/S"], "/TH")
            self.assertEqual(data["/Headers"][0], header["/ID"])

    def test_proven_neptune_placeholders_are_removed(self) -> None:
        original = _make_neptune_placeholder_table()
        repaired, result = repair_layout_tables(original)

        self.assertTrue(
            any(
                "Neptune empty header placeholders" in action
                for action in result.actions
            )
        )
        self.assertFalse(
            any("Neptune placeholder canonicalization" in item for item in result.unresolved)
        )
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            header_row, data_row = table["/K"]
            headers = header_row["/K"]
            self.assertEqual(len(headers), 2)
            self.assertEqual(
                [str(cell["/ID"]) for cell in headers],
                ["neptune-header-0", "neptune-header-2"],
            )
            self.assertEqual([cell["/S"] for cell in headers], ["/TH", "/TH"])
            self.assertEqual(
                [cell["/Scope"] for cell in headers],
                ["/Column", "/Column"],
            )
            self.assertEqual(
                [cell["/Headers"][0] for cell in headers],
                ["existing-header", "existing-header"],
            )
            self.assertTrue(
                all(cell["/P"].objgen == header_row.objgen for cell in headers)
            )
            self.assertEqual(len(data_row["/K"]), 4)
            self.assertEqual(
                [cell["/Headers"][0] for cell in data_row["/K"]],
                [
                    "neptune-header-0",
                    "neptune-header-0",
                    "neptune-header-2",
                    "neptune-header-2",
                ],
            )
            self.assertEqual(
                [cell["/A"][0]["/ColSpan"] for cell in headers],
                [2, 2],
            )
            self.assertEqual(
                [cell["/A"][0]["/ADBE_ColIndex"] for cell in headers],
                [0, 2],
            )

        repaired_again, second_result = repair_layout_tables(repaired)
        self.assertEqual(repaired_again, repaired)
        self.assertEqual(second_result.actions, [])

    def test_neptune_placeholder_near_miss_is_reported_and_retained(self) -> None:
        original = _make_neptune_placeholder_table(conflicting_header_index=True)
        repaired, result = repair_layout_tables(original)

        self.assertTrue(
            any(
                "Neptune placeholder canonicalization proof incomplete" in item
                for item in result.unresolved
            )
        )
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            self.assertEqual(len(table["/K"][0]["/K"]), 4)

    def test_neptune_out_of_place_placeholder_reference_is_retained(self) -> None:
        original = _make_neptune_placeholder_table()
        modified = io.BytesIO()
        with pikepdf.open(io.BytesIO(original)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            table["/K"][1]["/K"][0]["/Headers"] = pikepdf.Array(
                [pikepdf.String("neptune-header-1")]
            )
            pdf.save(modified)

        repaired, result = repair_layout_tables(modified.getvalue())

        self.assertTrue(
            any(
                "Neptune placeholder canonicalization proof incomplete" in item
                for item in result.unresolved
            )
        )
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            self.assertEqual(len(table["/K"][0]["/K"]), 4)

    def test_real_neptune_fixture_tables_are_canonicalized(self) -> None:
        if not MACRO_FIXTURE.exists():
            self.skipTest("Macroeconomics fixture PDF not available")

        original = MACRO_FIXTURE.read_bytes()
        repaired, result = repair_layout_tables(original)

        self.assertEqual(result.actions, [
            "table1: removed proven Neptune empty header placeholders",
            "table1: normalized /Summary attribute",
            "table2: removed proven Neptune empty header placeholders",
            "table2: normalized /Summary attribute",
        ])
        self.assertEqual(result.unresolved, [])
        with pikepdf.open(io.BytesIO(original)) as before, pikepdf.open(
            io.BytesIO(repaired)
        ) as after:
            before_tables = [
                node
                for node in _iter_tables(before.Root["/StructTreeRoot"])
                if node.get("/S") == "/Table"
            ]
            after_tables = [
                node
                for node in _iter_tables(after.Root["/StructTreeRoot"])
                if node.get("/S") == "/Table"
            ]
            for table in after_tables:
                self.assertNotIn("/Summary", table)
                self.assertTrue(
                    list(_owner_attribute_values(table, "/Summary", "/Table"))
                )
            self.assertEqual(len(before_tables), 2)
            self.assertEqual(len(after_tables), 2)
            for before_table, after_table in zip(before_tables, after_tables):
                before_rows, _ = _table_rows_and_cells(before_table)
                after_rows, _ = _table_rows_and_cells(after_table)
                self.assertEqual(_logical_row_widths(before_rows), [6, 4])
                self.assertEqual(_logical_row_widths(after_rows), [4, 4])
                self.assertEqual(
                    [str(cell["/ID"]) for cell in after_rows[0]["/K"]],
                    [str(before_rows[0]["/K"][0]["/ID"]),
                     str(before_rows[0]["/K"][2]["/ID"])],
                )
                self.assertEqual(
                    [
                        [str(value) for value in cell["/Headers"]]
                        for cell in after_rows[1]["/K"]
                    ],
                    [
                        [str(after_rows[0]["/K"][0]["/ID"])],
                        [str(after_rows[0]["/K"][0]["/ID"])],
                        [str(after_rows[0]["/K"][1]["/ID"])],
                        [str(after_rows[0]["/K"][1]["/ID"])],
                    ],
                )
                self.assertEqual(
                    [str(cell["/ID"]) for cell in before_rows[1]["/K"]],
                    [str(cell["/ID"]) for cell in after_rows[1]["/K"]],
                )
            self.assertEqual(len(before.pages), len(after.pages))
            self.assertEqual(
                [
                    bytes(page.obj["/Contents"])
                    for page in before.pages
                ],
                [
                    bytes(page.obj["/Contents"])
                    for page in after.pages
                ],
            )
        self.assertEqual(repair_layout_tables(repaired)[0], repaired)
        audit = audit_pdf_bytes(repaired)
        self.assertEqual(audit.logical_row_widths, {"table1": [4, 4], "table2": [4, 4]})
        self.assertEqual(audit.tables_without_th, 0)

    def test_real_tax_efficiency_layout_tables_are_unwrapped(self) -> None:
        fixture = MICRO_FIXTURE_DIR / "80ff0853.pdf"
        if not fixture.exists():
            self.skipTest("Microeconomics Tax Efficiency fixture PDF not available")

        original = fixture.read_bytes()
        repaired, result = repair_layout_tables(original)
        audit = audit_pdf_bytes(repaired)

        self.assertEqual(result.unwrapped_grid, 2)
        self.assertEqual(audit.table_count, 0)
        self.assertEqual(audit.tables_without_th, 0)
        self.assertEqual(audit.invalid_row_child_roles, [])
        with pikepdf.open(io.BytesIO(original)) as before, pikepdf.open(
            io.BytesIO(repaired)
        ) as after:
            self.assertEqual(len(before.pages), len(after.pages))
            self.assertEqual(
                [bytes(page.obj["/Contents"]) for page in before.pages],
                [bytes(page.obj["/Contents"]) for page in after.pages],
            )
        self.assertEqual(repair_layout_tables(repaired)[0], repaired)

    def test_word_one_row_table_without_terminal_border_span_is_retained(self) -> None:
        original = _make_table_pdf([[2, 2]])
        tagged_as_word = io.BytesIO()
        with pikepdf.open(io.BytesIO(original)) as pdf:
            pdf.docinfo["/Producer"] = pikepdf.String("Microsoft® Word 2010")
            pdf.save(tagged_as_word)

        repaired, result = repair_layout_tables(tagged_as_word.getvalue())

        self.assertEqual(result.unwrapped_grid, 0)
        self.assertEqual(result.ambiguous_tables, 1)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(
                pdf.Root["/StructTreeRoot"]["/K"][0]["/S"],
                "/Table",
            )

    def test_word_comparison_layout_signatures_are_unwrapped(self) -> None:
        for row_widths in ([2], [3], [2, 1]):
            with self.subTest(row_widths=row_widths):
                repaired, result = repair_layout_tables(
                    _make_word_border_table(row_widths, rich_layout=True)
                )

                self.assertEqual(result.unwrapped_grid, 1)
                self.assertEqual(audit_pdf_bytes(repaired).table_count, 0)

    def test_word_single_row_callout_boxes_are_unwrapped(self) -> None:
        # A lone /TR of plain /TD cells can never satisfy the headers rule,
        # so Word's bordered callout/equation boxes are unwrapped regardless
        # of how little content they hold.
        for row_widths in ([1], [2], [5]):
            with self.subTest(row_widths=row_widths):
                repaired, result = repair_layout_tables(
                    _make_word_border_table(row_widths)
                )

                self.assertEqual(
                    result.unwrapped_grid + result.unwrapped_1x1, 1
                )
                self.assertEqual(audit_pdf_bytes(repaired).table_count, 0)

    def test_word_fillin_box_with_lone_full_width_row_is_unwrapped(self) -> None:
        for row_widths in ([3, 1], [1, 3, 1]):
            with self.subTest(row_widths=row_widths):
                repaired, result = repair_layout_tables(
                    _make_word_border_table(row_widths)
                )

                self.assertEqual(result.unwrapped_grid, 1)
                self.assertEqual(audit_pdf_bytes(repaired).table_count, 0)

    def test_geometric_header_spans_from_text_positions(self) -> None:
        pdf = pikepdf.new()
        page = pdf.add_blank_page(page_size=(400, 400))
        stream = []
        next_mcid = 0
        xs_rows = [
            [10.0, 85.0],  # short header: second cell centered over cols 2-3
            [10.0, 60.0, 110.0],
            [10.0, 63.5, 110.0],
        ]
        cell_mcids: list[list[int]] = []
        for xs in xs_rows:
            row_mcids = []
            for x in xs:
                stream.append(
                    f"/P<</MCID {next_mcid}>> BDC BT 1 0 0 1 {x} 700 Tm (t) Tj ET EMC"
                )
                row_mcids.append(next_mcid)
                next_mcid += 1
            # trailing Word border span
            stream.append(f"/Span<</MCID {next_mcid}>> BDC 0 0 m 1 0 l S EMC")
            row_mcids.append(next_mcid)
            next_mcid += 1
            cell_mcids.append(row_mcids)
        page["/Contents"] = pdf.make_stream(" ".join(stream).encode("ascii"))

        rows = []
        for row_index, row_mcids in enumerate(cell_mcids):
            cells = [
                pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name("/TD"),
                        "/K": mcid,
                        "/Pg": page.obj,
                    }
                )
                for mcid in row_mcids[:-1]
            ]
            border = pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/Span"),
                    "/K": row_mcids[-1],
                    "/Pg": page.obj,
                }
            )
            rows.append(
                pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name("/TR"),
                        "/K": pikepdf.Array([*cells, border]),
                        "/Pg": page.obj,
                    }
                )
            )
        table = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Table"),
                "/K": pikepdf.Array(rows),
                "/Pg": page.obj,
            }
        )
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([pdf.make_indirect(table)]),
            }
        )
        pdf.docinfo["/Producer"] = pikepdf.String("Microsoft® Word 2010")
        buf = io.BytesIO()
        pdf.save(buf)

        repaired, result = repair_layout_tables(buf.getvalue())

        span_actions = [
            action for action in result.actions if "text geometry" in action
        ]
        self.assertEqual(
            span_actions,
            ["table1: derived header column spans [1, 2] from text geometry"],
        )
        audit = audit_pdf_bytes(repaired)
        self.assertEqual(audit.tables_without_th, 0)
        self.assertEqual(audit.data_cells_missing_headers, [])

    def test_word_stack_with_two_wide_rows_is_not_a_callout_box(self) -> None:
        repaired, result = repair_layout_tables(_make_word_border_table([5, 5, 1]))

        self.assertEqual(result.unwrapped_grid, 0)
        self.assertEqual(audit_pdf_bytes(repaired).table_count, 1)

    def test_word_two_row_equal_width_table_is_not_a_callout_box(self) -> None:
        repaired, result = repair_layout_tables(_make_word_border_table([2, 2]))

        self.assertEqual(result.unwrapped_grid, 0)
        self.assertEqual(audit_pdf_bytes(repaired).table_count, 1)

    def test_non_word_single_row_table_is_retained(self) -> None:
        original = _make_word_border_table([2])
        neutral = io.BytesIO()
        with pikepdf.open(io.BytesIO(original)) as pdf:
            pdf.docinfo["/Producer"] = pikepdf.String("Neutral Producer")
            pdf.save(neutral)

        repaired, result = repair_layout_tables(neutral.getvalue())

        self.assertEqual(result.unwrapped_grid + result.unwrapped_1x1, 0)
        self.assertEqual(audit_pdf_bytes(repaired).table_count, 1)

    def test_word_grouped_header_signature_repairs_two_tier_grid(self) -> None:
        repaired, result = repair_layout_tables(
            _make_word_border_table([4, 7, 7, 7, 7])
        )
        audit = audit_pdf_bytes(repaired)

        self.assertEqual(result.annotated_data_tables, 1)
        self.assertEqual(audit.tables_without_th, 0)
        self.assertEqual(audit.invalid_row_child_roles, [])
        self.assertEqual(audit.logical_row_widths, {"table1": [7, 7, 7, 7, 7]})
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            self.assertEqual(
                [cell["/S"] for cell in table["/K"][0]["/K"]],
                ["/TH"] * 4,
            )
            grouped_headers = list(table["/K"][0]["/K"])[1:]
            self.assertTrue(
                all(
                    "/ColSpan" not in cell
                    and cell["/A"]["/O"] == "/Table"
                    and cell["/A"]["/ColSpan"] == 2
                    for cell in grouped_headers
                )
            )
            self.assertEqual(
                [cell["/S"] for cell in table["/K"][1]["/K"]],
                ["/TH"] * 7,
            )
        self.assertEqual(repair_layout_tables(repaired)[0], repaired)

    def test_real_tax_equity_layouts_and_grouped_headers_are_repaired(self) -> None:
        fixture = MICRO_FIXTURE_DIR / "64e45033.pdf"
        if not fixture.exists():
            self.skipTest("Microeconomics Tax Equity fixture PDF not available")

        original = fixture.read_bytes()
        repaired, result = repair_layout_tables(original)
        audit = audit_pdf_bytes(repaired)

        self.assertEqual(result.unwrapped_grid, 3)
        self.assertEqual(result.annotated_data_tables, 1)
        self.assertEqual(audit.table_count, 1)
        self.assertEqual(audit.tables_without_th, 0)
        self.assertEqual(audit.invalid_row_child_roles, [])
        self.assertEqual(audit.tables_with_inconsistent_row_widths, [])
        self.assertEqual(audit.logical_row_widths, {"table1": [7, 7, 7, 7, 7]})
        with pikepdf.open(io.BytesIO(original)) as before, pikepdf.open(
            io.BytesIO(repaired)
        ) as after:
            tables = [
                node
                for node in _iter_tables(after.Root["/StructTreeRoot"])
                if node.get("/S") == "/Table"
            ]
            self.assertEqual(len(tables), 1)
            rows = tables[0]["/K"]
            self.assertEqual(
                [cell["/S"] for cell in rows[0]["/K"]],
                ["/TH"] * 4,
            )
            self.assertEqual(
                [
                    int(cell["/A"].get("/ColSpan", 1))
                    if isinstance(cell.get("/A"), pikepdf.Dictionary)
                    else 1
                    for cell in rows[0]["/K"]
                ],
                [1, 2, 2, 2],
            )
            self.assertTrue(
                all(
                    "/ColSpan" not in cell
                    and cell["/A"]["/O"] == "/Table"
                    for cell in list(rows[0]["/K"])[1:]
                )
            )
            self.assertEqual(
                [cell["/S"] for cell in rows[1]["/K"]],
                ["/TH"] * 7,
            )
            self.assertTrue(
                all(
                    cell["/S"] == "/TD" and len(cell["/Headers"]) == 2
                    for row in list(rows)[2:]
                    for cell in row["/K"]
                )
            )
            self.assertEqual(len(before.pages), len(after.pages))
            self.assertEqual(
                [bytes(page.obj["/Contents"]) for page in before.pages],
                [bytes(page.obj["/Contents"]) for page in after.pages],
            )
        self.assertEqual(repair_layout_tables(repaired)[0], repaired)

    def test_word_grouped_header_near_miss_is_retained(self) -> None:
        fixture = MICRO_FIXTURE_DIR / "64e45033.pdf"
        if not fixture.exists():
            self.skipTest("Microeconomics Tax Equity fixture PDF not available")

        modified = io.BytesIO()
        with pikepdf.open(fixture) as pdf:
            tables = [
                node
                for node in _iter_tables(pdf.Root["/StructTreeRoot"])
                if node.get("/S") == "/Table"
            ]
            first_header = tables[3]["/K"][0]["/K"][0]
            first_header["/K"] = pikepdf.Array([first_header["/K"][0], 999])
            pdf.save(modified)

        repaired, result = repair_layout_tables(modified.getvalue())

        self.assertFalse(
            any("Word grouped headers" in action for action in result.actions)
        )
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            tables = [
                node
                for node in _iter_tables(pdf.Root["/StructTreeRoot"])
                if node.get("/S") == "/Table"
            ]
            self.assertEqual(len(tables), 1)
            self.assertEqual(
                [len(row["/K"]) for row in tables[0]["/K"]],
                [5, 8, 8, 8, 8],
            )

    def test_real_pdf_lib_overlapping_layout_tables_are_unwrapped(self) -> None:
        if not COLLEGE_ALGEBRA_FIXTURE.exists():
            self.skipTest("College Algebra Graphing Polynomial Functions fixture unavailable")

        original = COLLEGE_ALGEBRA_FIXTURE.read_bytes()
        repaired, result = repair_layout_tables(original)
        audit = audit_pdf_bytes(repaired)

        self.assertEqual(result.unwrapped_grid, 2)
        self.assertEqual(result.ambiguous_tables, 0)
        self.assertTrue(
            all(
                f"table{index}: unwrapped overlapping pdf-lib layout to /Sect"
                in result.actions
                for index in (1, 2)
            )
        )
        self.assertEqual(audit.tables_with_inconsistent_row_widths, [])
        self.assertEqual(
            audit.logical_row_widths,
            {
                "table1": [11] * 11,
            },
        )
        with pikepdf.open(io.BytesIO(original)) as before, pikepdf.open(
            io.BytesIO(repaired)
        ) as after:
            tables = [
                node
                for node in _iter_tables(after.Root["/StructTreeRoot"])
                if node.get("/S") == "/Table"
            ]
            self.assertEqual(len(tables), 1)
            self.assertEqual(_logical_row_widths(_table_rows_and_cells(tables[0])[0]), [11] * 11)
            self.assertEqual(
                [bytes(page.obj["/Contents"]) for page in before.pages],
                [bytes(page.obj["/Contents"]) for page in after.pages],
            )
        self.assertEqual(repair_layout_tables(repaired)[0], repaired)

    def test_pdf_lib_overlapping_layout_signature_is_unwrapped(self) -> None:
        repaired, result = repair_layout_tables(_make_pdf_lib_overlapping_table())

        self.assertEqual(result.unwrapped_grid, 1)
        self.assertEqual(audit_pdf_bytes(repaired).table_count, 0)
        self.assertEqual(repair_layout_tables(repaired)[0], repaired)

    def test_pdf_lib_overlapping_layout_near_miss_is_retained(self) -> None:
        repaired, result = repair_layout_tables(
            _make_pdf_lib_overlapping_table(valid_indices=False)
        )

        self.assertEqual(result.unwrapped_grid, 0)
        self.assertEqual(result.ambiguous_tables, 1)
        self.assertEqual(audit_pdf_bytes(repaired).table_count, 1)

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
