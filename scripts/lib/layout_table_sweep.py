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


def _is_layout_table(
    rows: list[pikepdf.Dictionary],
    cells: list[pikepdf.Dictionary],
    table: pikepdf.Dictionary,
) -> bool:
    summary = table.get("/Summary")
    if summary is not None and len(str(summary).strip()) > 40:
        return False
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


def _row_direct_cells(row: pikepdf.Dictionary) -> list[pikepdf.Dictionary]:
    kids = row.get("/K")
    if not isinstance(kids, pikepdf.Array):
        return []
    return [kid for kid in kids if isinstance(kid, pikepdf.Dictionary)]


def _row_cell_counts(table: pikepdf.Dictionary) -> list[int]:
    rows = [
        node
        for node in _iter_dict_nodes(table)
        if node.get("/S") == "/TR" and node is not table
    ]
    return [len(_row_direct_cells(row)) for row in rows]


def _is_two_row_header_table(rows: list[pikepdf.Dictionary]) -> bool:
    if len(rows) != 2:
        return False
    header_count = len(_row_direct_cells(rows[0]))
    data_count = len(_row_direct_cells(rows[1]))
    return header_count > 0 and data_count > header_count and data_count % header_count == 0


def _is_irregular_table(table: pikepdf.Dictionary) -> bool:
    rows = [
        node
        for node in _iter_dict_nodes(table)
        if node.get("/S") == "/TR" and node is not table
    ]
    if _is_two_row_header_table(rows):
        return False
    counts = _row_cell_counts(table)
    if not counts:
        return True
    return len(set(counts)) > 1


def _collect_table_mcids(table: pikepdf.Dictionary) -> list[int]:
    mcids: list[int] = []
    for node in _iter_dict_nodes(table):
        if node is table:
            continue
        content = node.get("/K")
        if isinstance(content, int):
            mcids.append(content)
        elif isinstance(content, pikepdf.Array):
            for item in content:
                if isinstance(item, int):
                    mcids.append(item)
    return mcids


def _revert_table_to_figure(table: pikepdf.Dictionary, *, index: int) -> None:
    summary = str(table.get("/Summary") or "").strip()
    contents = str(table.get("/Contents") or "").strip()
    alt = contents or summary

    headers: list[str] = []
    for row in _iter_dict_nodes(table):
        if row is table or row.get("/S") != "/TR":
            continue
        kids = row.get("/K")
        if not isinstance(kids, pikepdf.Array):
            continue
        for cell in kids:
            if not isinstance(cell, pikepdf.Dictionary):
                continue
            if cell.get("/S") == "/TH":
                cell_text = cell.get("/K")
                if cell_text is not None and not isinstance(cell_text, (int, pikepdf.Array)):
                    headers.append(str(cell_text).strip())

    if headers and (not alt or alt == f"Table {index}"):
        alt = (
            "Table with columns labeled "
            + ", ".join(f"'{header}'" for header in headers)
            + "."
        )
    if not alt or alt == f"Table {index}":
        alt = "Table"

    mcids = _collect_table_mcids(table)
    table["/S"] = pikepdf.Name("/Figure")
    table["/Alt"] = pikepdf.String(alt)
    table["/Contents"] = pikepdf.String(alt)
    _remove_key(table, "/Summary")
    table["/K"] = pikepdf.Array(mcids) if mcids else pikepdf.Array([])
    existing_class = table.get("/C")
    if existing_class is None:
        table["/C"] = pikepdf.Name("/table-figure-reverted")
    elif isinstance(existing_class, pikepdf.Array):
        table["/C"] = pikepdf.Array(["/table-figure-reverted", *existing_class])
    else:
        table["/C"] = pikepdf.Array(["/table-figure-reverted", existing_class])


def _ensure_summary(table: pikepdf.Dictionary, index: int) -> None:
    summary = table.get("/Summary")
    if summary is None or len(str(summary).strip()) <= 40:
        table["/Summary"] = pikepdf.String(f"Table {index}")


def _promote_to_th(cell: pikepdf.Dictionary, *, cell_id: str) -> None:
    if cell.get("/S") in ("/TD", "/Span"):
        cell["/S"] = pikepdf.Name("/TH")
    cell["/Scope"] = pikepdf.Name("/Column")
    cell["/ID"] = pikepdf.String(cell_id)


def _ensure_td(cell: pikepdf.Dictionary) -> None:
    if cell.get("/S") == "/Span":
        cell["/S"] = pikepdf.Name("/TD")


def _make_empty_th(page: pikepdf.Object | None, cell_id: str) -> pikepdf.Dictionary:
    cell = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/TH"),
            "/Scope": pikepdf.Name("/Column"),
            "/ID": pikepdf.String(cell_id),
            "/K": pikepdf.Array([]),
        }
    )
    if page is not None:
        cell["/Pg"] = page
    return cell


def _annotate_two_row_header_table(
    table: pikepdf.Dictionary,
    rows: list[pikepdf.Dictionary],
    index: int,
) -> None:
    _ensure_summary(table, index)
    header_row = rows[0]
    header_cells = _row_direct_cells(header_row)
    data_cells = _row_direct_cells(rows[1])
    if not header_cells or not data_cells:
        return

    colspan = len(data_cells) // len(header_cells)
    expanded_header_cells: list[pikepdf.Dictionary] = []
    th_ids: list[str] = []

    for group_index, header_cell in enumerate(header_cells):
        page = header_cell.get("/Pg")
        for span_index in range(colspan):
            column_index = group_index * colspan + span_index
            cell_id = f"tbl{index}-h{column_index}"
            if span_index == 0:
                cell = header_cell
                _promote_to_th(cell, cell_id=cell_id)
                _remove_key(cell, "/Attributes")
            else:
                cell = _make_empty_th(page, cell_id)
            expanded_header_cells.append(cell)
            th_ids.append(cell_id)

    header_row["/K"] = pikepdf.Array(expanded_header_cells)

    for data_index, cell in enumerate(data_cells):
        _ensure_td(cell)
        if data_index < len(th_ids):
            cell["/Headers"] = pikepdf.Array([pikepdf.String(th_ids[data_index])])


def _annotate_data_table(table: pikepdf.Dictionary, rows: list[pikepdf.Dictionary], index: int) -> None:
    _ensure_summary(table, index)
    if not rows:
        return
    if any(
        cell.get("/S") == "/TH"
        for row in rows
        for cell in _row_direct_cells(row)
    ):
        return

    header_cells = _row_direct_cells(rows[0])
    th_ids: list[str] = []
    for cell_index, cell in enumerate(header_cells):
        cell_id = f"tbl{index}-h{cell_index}"
        _promote_to_th(cell, cell_id=cell_id)
        th_ids.append(cell_id)

    for row in rows[1:]:
        for col_index, cell in enumerate(_row_direct_cells(row)):
            _ensure_td(cell)
            if col_index < len(th_ids):
                cell["/Headers"] = pikepdf.Array([pikepdf.String(th_ids[col_index])])


def repair_layout_tables(pdf_bytes: bytes) -> tuple[bytes, LayoutTableRepairResult]:
    unwrapped_1x1 = 0
    unwrapped_grid = 0
    annotated_data_tables = 0
    reverted_irregular = 0
    actions: list[str] = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            result = LayoutTableRepairResult(0, 0, 0, 0, [])
            return pdf_bytes, result

        tables = [node for node in _iter_dict_nodes(struct_root) if node.get("/S") == "/Table"]
        for index, table in enumerate(tables, start=1):
            rows, cells = _table_rows_and_cells(table)
            if _is_layout_table(rows, cells, table):
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
            elif _is_two_row_header_table(rows):
                _annotate_two_row_header_table(table, rows, index)
                annotated_data_tables += 1
                actions.append(f"table{index}: annotated two-row header table")
            elif _is_irregular_table(table):
                _revert_table_to_figure(table, index=index)
                reverted_irregular += 1
                actions.append(f"table{index}: reverted irregular /Table to /Figure")
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
