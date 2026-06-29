"""Repair layout regions mis-tagged as /Table in remediated preview PDFs."""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass

import pikepdf


@dataclass
class LayoutTableRepairResult:
    tables_found: int
    unwrapped_1x1: int
    unwrapped_grid: int
    annotated_data_tables: int
    actions: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _iter_dict_nodes(obj: pikepdf.Object):
    if not isinstance(obj, pikepdf.Dictionary):
        return
    yield obj
    kids = obj.get("/K")
    if isinstance(kids, pikepdf.Array):
        for kid in kids:
            if isinstance(kid, pikepdf.Dictionary):
                yield from _iter_dict_nodes(kid)
            elif isinstance(kid, pikepdf.Array):
                for nested in kid:
                    if isinstance(nested, pikepdf.Dictionary):
                        yield from _iter_dict_nodes(nested)
    elif isinstance(kids, pikepdf.Dictionary):
        yield from _iter_dict_nodes(kids)


def _table_rows_and_cells(table: pikepdf.Dictionary) -> tuple[list[pikepdf.Dictionary], list[pikepdf.Dictionary]]:
    rows = [node for node in _iter_dict_nodes(table) if node.get("/S") == "/TR" and node is not table]
    cells = [
        node
        for node in _iter_dict_nodes(table)
        if node.get("/S") in ("/TD", "/TH") and node is not table
    ]
    return rows, cells


def _is_layout_table(rows: list[pikepdf.Dictionary], cells: list[pikepdf.Dictionary]) -> bool:
    if len(rows) == 1 and len(cells) == 1:
        return True
    if len(rows) <= 2 and len(cells) <= 4:
        return True
    return False


def _remove_key(obj: pikepdf.Dictionary, key: str) -> None:
    if key in obj:
        del obj[key]


def _unwrap_single_cell_table(table: pikepdf.Dictionary, cell: pikepdf.Dictionary) -> None:
    table["/S"] = pikepdf.Name("/Sect")
    content = cell.get("/K")
    table["/K"] = content if content is not None else pikepdf.Array([])
    _remove_key(table, "/Summary")
    _remove_key(table, "/Alt")


def _unwrap_grid_table(table: pikepdf.Dictionary, cells: list[pikepdf.Dictionary]) -> None:
    divs = pikepdf.Array()
    for cell in cells:
        div = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Div"),
                "/K": cell.get("/K") if cell.get("/K") is not None else pikepdf.Array([]),
            }
        )
        if cell.get("/Pg") is not None:
            div["/Pg"] = cell["/Pg"]
        divs.append(div)
    table["/S"] = pikepdf.Name("/Sect")
    table["/K"] = divs
    _remove_key(table, "/Summary")
    _remove_key(table, "/Alt")


def _annotate_data_table(table: pikepdf.Dictionary, rows: list[pikepdf.Dictionary], index: int) -> None:
    table["/Summary"] = pikepdf.String(f"Table {index}")
    first_row = rows[0]
    for cell in _iter_dict_nodes(first_row):
        if cell.get("/S") == "/TD":
            cell["/S"] = pikepdf.Name("/TH")
            cell["/Scope"] = pikepdf.Name("/Column")


def repair_layout_tables(pdf_bytes: bytes) -> tuple[bytes, LayoutTableRepairResult]:
    unwrapped_1x1 = 0
    unwrapped_grid = 0
    annotated_data_tables = 0
    actions: list[str] = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            result = LayoutTableRepairResult(0, 0, 0, 0, [])
            return pdf_bytes, result

        tables = [node for node in _iter_dict_nodes(struct_root) if node.get("/S") == "/Table"]
        for index, table in enumerate(tables, start=1):
            rows, cells = _table_rows_and_cells(table)
            if _is_layout_table(rows, cells):
                if len(rows) == 1 and len(cells) == 1:
                    _unwrap_single_cell_table(table, cells[0])
                    unwrapped_1x1 += 1
                    actions.append(f"table{index}: unwrapped 1x1 layout to /Sect")
                else:
                    _unwrap_grid_table(table, cells)
                    unwrapped_grid += 1
                    actions.append(
                        f"table{index}: unwrapped {len(rows)}x{len(cells)} layout grid to /Sect"
                    )
            else:
                _annotate_data_table(table, rows, index)
                annotated_data_tables += 1
                actions.append(f"table{index}: added /Summary and header cells")

        output = io.BytesIO()
        pdf.save(output)
        result = LayoutTableRepairResult(
            tables_found=len(tables),
            unwrapped_1x1=unwrapped_1x1,
            unwrapped_grid=unwrapped_grid,
            annotated_data_tables=annotated_data_tables,
            actions=actions,
        )
        return output.getvalue(), result
