"""Repair generic PDF table structure without discarding table semantics."""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from numbers import Integral

import pikepdf


@dataclass
class LayoutTableRepairResult:
    tables_found: int
    unwrapped_1x1: int
    unwrapped_grid: int
    annotated_data_tables: int
    actions: list[str]
    conflicts: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    ambiguous_tables: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _GridCell:
    cell: pikepdf.Dictionary
    row: int
    col: int
    col_span: int
    row_span: int

    @property
    def columns(self) -> range:
        return range(self.col, self.col + self.col_span)

    @property
    def last_row(self) -> int:
        return self.row + self.row_span - 1


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


def _table_rows_and_cells(
    table: pikepdf.Dictionary,
) -> tuple[list[pikepdf.Dictionary], list[pikepdf.Dictionary]]:
    rows = [
        node
        for node in _iter_dict_nodes(table)
        if node.get("/S") == "/TR" and node is not table
    ]
    cells = [
        node
        for node in _iter_dict_nodes(table)
        if node.get("/S") in ("/TD", "/TH", "/Span") and node is not table
    ]
    return rows, cells


def _row_direct_cells(row: pikepdf.Dictionary) -> list[pikepdf.Dictionary]:
    kids = row.get("/K")
    if not isinstance(kids, pikepdf.Array):
        return []
    return [
        kid
        for kid in kids
        if isinstance(kid, pikepdf.Dictionary)
        and kid.get("/S") in ("/TD", "/TH", "/Span")
    ]


def _as_positive_int(value: object) -> int | None:
    if isinstance(value, Integral):
        return max(1, int(value))
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return max(1, parsed)


def _attribute_values(cell: pikepdf.Dictionary, key: str):
    """Yield values from both common and producer-specific attribute entries."""
    for container_key in (key, "/Attributes", "/A"):
        value = cell.get(container_key)
        values = list(value) if isinstance(value, pikepdf.Array) else [value]
        for item in values:
            if container_key == key and item is not None and not isinstance(
                item, pikepdf.Dictionary
            ):
                yield item
                continue
            if isinstance(item, pikepdf.Dictionary):
                direct = item.get(key)
                if direct is not None:
                    yield direct
                nested = item.get("/O")
                if nested == "/Table":
                    direct = item.get(key)
                    if direct is not None:
                        yield direct


def _owner_attribute_values(
    obj: pikepdf.Dictionary,
    key: str,
    owner: str,
):
    for container_key in ("/A", "/Attributes"):
        value = obj.get(container_key)
        values = list(value) if isinstance(value, pikepdf.Array) else [value]
        for item in values:
            if isinstance(item, pikepdf.Dictionary) and item.get("/O") == owner:
                attribute = item.get(key)
                if attribute is not None:
                    yield attribute


def _as_nonnegative_int(value: object) -> int | None:
    if isinstance(value, Integral):
        parsed = int(value)
    else:
        try:
            parsed = int(str(value))
        except (TypeError, ValueError):
            return None
    return parsed if parsed >= 0 else None


def _single_nonnegative_attribute(values: list[object]) -> int | None:
    parsed = [_as_nonnegative_int(value) for value in values]
    if not parsed or any(value is None for value in parsed):
        return None
    unique = set(parsed)
    return parsed[0] if len(unique) == 1 else None


def _span(cell: pikepdf.Dictionary, key: str, *, conflicts: list[str], label: str) -> int:
    values = list(_attribute_values(cell, key))
    if not values:
        return 1
    parsed = [_as_positive_int(value) for value in values]
    valid = [value for value in parsed if value is not None]
    if not valid:
        conflicts.append(f"{label}: invalid {key} preserved; treated as 1")
        return 1
    if len(set(valid)) > 1:
        conflicts.append(f"{label}: conflicting {key} values preserved; used {valid[0]}")
    return valid[0]


def _build_grid(
    rows: list[pikepdf.Dictionary],
    *,
    conflicts: list[str],
) -> tuple[list[_GridCell], int, bool]:
    placed: list[_GridCell] = []
    occupied: dict[int, int] = {}
    malformed = False
    width = 0
    for row_index, row in enumerate(rows):
        col = 0
        cells = _row_direct_cells(row)
        for cell_index, cell in enumerate(cells):
            while occupied.get(col, -1) >= row_index:
                col += 1
            label = f"row {row_index + 1} cell {cell_index + 1}"
            col_span = _span(cell, "/ColSpan", conflicts=conflicts, label=label)
            row_span = _span(cell, "/RowSpan", conflicts=conflicts, label=label)
            while any(occupied.get(candidate, -1) >= row_index for candidate in range(col, col + col_span)):
                col += 1
            grid_cell = _GridCell(cell, row_index, col, col_span, row_span)
            placed.append(grid_cell)
            for candidate in grid_cell.columns:
                occupied[candidate] = max(occupied.get(candidate, -1), grid_cell.last_row)
            col += col_span
            width = max(width, col)
        if not cells:
            malformed = True
            conflicts.append(f"row {row_index + 1}: no direct cells")
    return placed, width, malformed


def _names(value: object) -> list[str]:
    if isinstance(value, pikepdf.Array):
        result: list[str] = []
        for item in value:
            result.extend(_names(item))
        return result
    if value is None:
        return []
    return [str(value).lstrip("/").lower()]


def _is_explicit_layout_table(table: pikepdf.Dictionary) -> bool:
    markers = {
        "layout",
        "layouttable",
        "layout-table",
        "presentation",
        "decorative",
        "spacer",
    }
    for key in ("/C", "/Role", "/Type"):
        if any(name in markers for name in _names(table.get(key))):
            return True
    for key in ("/Summary", "/Alt", "/Contents"):
        value = str(table.get(key) or "").strip().lower()
        if re.search(r"\b(layout|presentation|decorative|spacer)\b", value):
            return True
    return False


def _logical_row_widths(rows: list[pikepdf.Dictionary]) -> list[int] | None:
    grid, _width, malformed = _build_grid(rows, conflicts=[])
    if malformed:
        return None
    widths = [0] * len(rows)
    for item in grid:
        widths[item.row] = max(widths[item.row], item.col + item.col_span)
    return widths


def _is_adobe_layout_table(
    table: pikepdf.Dictionary,
    rows: list[pikepdf.Dictionary],
    cells: list[pikepdf.Dictionary],
) -> bool:
    """Recognize Neptune's irregular conversion-diagram table wrapper."""
    if len(rows) < 3 or len(set(len(_row_direct_cells(row)) for row in rows)) != 1:
        return False
    if len(_row_direct_cells(rows[0])) != 3:
        return False
    if len(cells) != len(rows) * 3:
        return False

    declared_columns = _single_nonnegative_attribute(
        list(_owner_attribute_values(table, "/ADBE_NumCol", "/Table"))
    )
    if declared_columns is None or declared_columns < 2:
        return False
    declared_rows = _single_nonnegative_attribute(
        list(_owner_attribute_values(table, "/ADBE_NumRow", "/Table"))
    )
    if declared_rows is not None and declared_rows != len(rows):
        return False
    processes = {
        str(value).lstrip("/")
        for value in _owner_attribute_values(table, "/ADBE_TableProcess", "/ADBE_Table")
    }
    if "Neptune" not in processes:
        return False

    row_indices: list[tuple[int, ...]] = []
    for row in rows:
        direct_cells = _row_direct_cells(row)
        indices = [
            _single_nonnegative_attribute(
                list(_owner_attribute_values(cell, "/ADBE_ColIndex", "/Table"))
            )
            for cell in direct_cells
        ]
        if any(index is None for index in indices):
            return False
        valid_indices = tuple(index for index in indices if index is not None)
        if len(set(valid_indices)) != len(valid_indices) or any(
            index >= declared_columns for index in valid_indices
        ):
            return False
        row_indices.append(valid_indices)
    if len(set(row_indices)) < 2:
        return False
    if not any(
        max(indices) - min(indices) + 1 > len(set(indices))
        for indices in row_indices
    ):
        return False

    logical_widths = _logical_row_widths(rows)
    if logical_widths is None or len(set(logical_widths)) < 2:
        return False
    if max(logical_widths) != declared_columns:
        return False

    empty_cells = [
        cell
        for cell in cells
        if cell.get("/K") is None
        or isinstance(cell.get("/K"), pikepdf.Array) and len(cell["/K"]) == 0
    ]
    if len(empty_cells) < 2:
        return False
    grid, _width, malformed = _build_grid(rows, conflicts=[])
    if malformed or any(item.row_span != 1 for item in grid):
        return False
    return any(
        item.col_span > 1
        and not item.cell.get("/K")
        and any(
            candidate.row == item.row and candidate.col > item.col
            for candidate in grid
        )
        for item in grid
    )


def _remove_key(obj: pikepdf.Dictionary, key: str) -> None:
    if key in obj:
        del obj[key]


def _ensure_summary(
    table: pikepdf.Dictionary,
    index: int,
    changed: list[bool],
) -> None:
    summary = str(table.get("/Summary") or "").strip()
    if not summary:
        table["/Summary"] = pikepdf.String(f"Table {index}")
        changed[0] = True


def _set_missing_parent(
    child: pikepdf.Dictionary,
    parent: pikepdf.Dictionary,
) -> bool:
    """Avoid direct-object cycles in synthetic/unit-test structure trees."""
    objgen = getattr(parent, "objgen", None)
    if child.get("/P") is not None or not isinstance(objgen, tuple) or objgen == (0, 0):
        return False
    child["/P"] = parent
    return True


def _unwrap_single_cell_table(
    table: pikepdf.Dictionary,
    rows: list[pikepdf.Dictionary],
    cell: pikepdf.Dictionary,
) -> None:
    table["/S"] = pikepdf.Name("/Sect")
    table["/K"] = pikepdf.Array(rows or [cell])
    for row in rows:
        row["/S"] = pikepdf.Name("/Div")
    if cell.get("/S") in ("/TD", "/TH"):
        cell["/S"] = pikepdf.Name("/Span")
    _remove_key(table, "/Summary")
    _remove_key(table, "/Alt")


def _unwrap_grid_table(
    table: pikepdf.Dictionary,
    rows: list[pikepdf.Dictionary],
    cells: list[pikepdf.Dictionary],
) -> None:
    table["/S"] = pikepdf.Name("/Sect")
    table["/K"] = pikepdf.Array(rows or cells)
    for row in rows:
        row["/S"] = pikepdf.Name("/Div")
    for cell in cells:
        if cell.get("/S") in ("/TD", "/TH"):
            cell["/S"] = pikepdf.Name("/Span")
    _remove_key(table, "/Summary")
    _remove_key(table, "/Alt")


def _all_existing_ids(
    tables: list[pikepdf.Dictionary],
) -> Counter[str]:
    ids: Counter[str] = Counter()
    for table in tables:
        for node in _iter_dict_nodes(table):
            value = node.get("/ID")
            if value is not None and str(value).strip():
                ids[str(value).strip()] += 1
    return ids


def _next_id(index: int, sequence: int, used: set[str]) -> str:
    candidate = f"tbl{index}-h{sequence}"
    while candidate in used:
        sequence += 1
        candidate = f"tbl{index}-h{sequence}"
    return candidate


def _ensure_header(
    cell: pikepdf.Dictionary,
    *,
    index: int,
    sequence: int,
    used_ids: set[str],
    existing_id_counts: Counter[str],
    conflicts: list[str],
    changed: list[bool],
) -> tuple[str | None, int]:
    existing = str(cell.get("/ID") or "").strip()
    if existing and existing_id_counts[existing] == 1 and existing not in used_ids:
        cell_id = existing
        used_ids.add(cell_id)
    else:
        if existing:
            conflicts.append(f"table{index}: duplicate/conflicting header /ID {existing!r} preserved by replacement")
        cell_id = _next_id(index, sequence, used_ids)
        cell["/ID"] = pikepdf.String(cell_id)
        used_ids.add(cell_id)
        changed[0] = True
    return cell_id, sequence + 1


def _scope(cell: pikepdf.Dictionary) -> str | None:
    value = str(cell.get("/Scope") or "").lstrip("/")
    return value if value in {"Column", "Row"} else None


def _promote_to_th(
    cell: pikepdf.Dictionary,
    *,
    scope: str,
    conflicts: list[str],
    changed: list[bool],
) -> None:
    current = str(cell.get("/S") or "").lstrip("/")
    if current in {"TD", "Span"}:
        cell["/S"] = pikepdf.Name("/TH")
        changed[0] = True
    elif current != "TH":
        conflicts.append(f"cell: unsupported structure role {current!r} prevented header promotion")
        return
    current_scope = _scope(cell)
    if current_scope is None:
        if cell.get("/Scope") is not None:
            conflicts.append(f"cell: invalid /Scope preserved; inferred /{scope}")
        cell["/Scope"] = pikepdf.Name(f"/{scope}")
        changed[0] = True
    elif current_scope != scope:
        conflicts.append(
            f"cell: existing /Scope /{current_scope} preserved instead of inferred /{scope}"
        )


def _ensure_td(cell: pikepdf.Dictionary, changed: list[bool]) -> None:
    if cell.get("/S") == "/Span":
        cell["/S"] = pikepdf.Name("/TD")
        changed[0] = True


def _make_empty_th(
    page: pikepdf.Object | None,
    parent: pikepdf.Dictionary,
) -> pikepdf.Dictionary:
    cell = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/TH"),
            "/Scope": pikepdf.Name("/Column"),
            "/K": pikepdf.Array([]),
        }
    )
    _set_missing_parent(cell, parent)
    if page is not None:
        cell["/Pg"] = page
    return cell


def _copy_direct_cell(cell: pikepdf.Dictionary) -> pikepdf.Dictionary:
    """Give direct test/producers' cells independent identities before reusing them."""
    objgen = getattr(cell, "objgen", None)
    if isinstance(objgen, tuple) and objgen == (0, 0):
        return pikepdf.Dictionary({key: value for key, value in cell.items()})
    return cell


def _headers_value(cell: pikepdf.Dictionary) -> list[str]:
    value = cell.get("/Headers")
    values = list(value) if isinstance(value, pikepdf.Array) else [value]
    return [str(item).strip() for item in values if item is not None and str(item).strip()]


def _associate_headers(
    cell: pikepdf.Dictionary,
    references: list[str],
    *,
    label: str,
    known_ids: set[str],
    conflicts: list[str],
    changed: list[bool],
) -> None:
    if not references:
        return
    existing = _headers_value(cell)
    unknown = [reference for reference in existing if reference not in known_ids]
    if unknown:
        conflicts.append(f"{label}: unresolved /Headers reference(s) {unknown!r} preserved")
    inapplicable = [
        reference
        for reference in existing
        if reference in known_ids and reference not in references
    ]
    if inapplicable:
        conflicts.append(
            f"{label}: non-applicable /Headers reference(s) {inapplicable!r} preserved"
        )
    missing = [reference for reference in references if reference not in existing]
    if not missing:
        return
    if cell.get("/Headers") is not None and not isinstance(cell.get("/Headers"), pikepdf.Array):
        conflicts.append(f"{label}: non-array /Headers conflict preserved")
        return
    cell["/Headers"] = pikepdf.Array(
        [pikepdf.String(reference) for reference in existing + missing]
    )
    changed[0] = True


def _expand_compact_header(
    rows: list[pikepdf.Dictionary],
    grid: list[_GridCell],
    *,
    index: int,
) -> bool:
    """Expand a producer's unspanned short header row when the ratio is exact."""
    if len(rows) < 2:
        return False
    first = _row_direct_cells(rows[0])
    second = _row_direct_cells(rows[1])
    if not first or len(second) <= len(first) or len(second) % len(first):
        return False
    if any(
        _span(cell, "/ColSpan", conflicts=[], label="header") != 1
        or _span(cell, "/RowSpan", conflicts=[], label="header") != 1
        for cell in first
    ):
        return False
    if any(cell.get("/S") == "/TH" and _scope(cell) == "Row" for cell in first):
        return False
    repeat = len(second) // len(first)
    expanded: list[pikepdf.Dictionary] = []
    for source in first:
        source = _copy_direct_cell(source)
        expanded.append(source)
        for _ in range(repeat - 1):
            copy = _make_empty_th(source.get("/Pg"), rows[0])
            expanded.append(copy)
    rows[0]["/K"] = pikepdf.Array(expanded)
    return True


def _annotate_data_table(
    table: pikepdf.Dictionary,
    rows: list[pikepdf.Dictionary],
    index: int,
    *,
    existing_id_counts: Counter[str],
    conflicts: list[str],
    unresolved: list[str],
) -> bool:
    if len(rows) < 2:
        unresolved.append(f"table{index}: fewer than two rows; retained as ambiguous /Table")
        return False
    changed = [False]
    grid, width, malformed = _build_grid(rows, conflicts=conflicts)
    if malformed or width < 1 or not grid:
        unresolved.append(f"table{index}: logical grid could not be derived; retained as /Table")
        return False
    if _expand_compact_header(rows, grid, index=index):
        changed[0] = True
        grid, width, malformed = _build_grid(rows, conflicts=conflicts)
    logical_widths = _logical_row_widths(rows)
    if logical_widths is None or len(set(logical_widths)) > 1:
        unresolved.append(
            f"table{index}: differing logical row widths; retained as ambiguous /Table"
        )
        return False

    row_grid: dict[int, list[_GridCell]] = {}
    for item in grid:
        row_grid.setdefault(item.row, []).append(item)
    headers = [item for item in grid if item.cell.get("/S") == "/TH"]
    if not headers:
        headers = [item for item in grid if item.row == 0]
    else:
        column_headers = [
            item
            for item in headers
            if (_scope(item.cell) or ("Column" if item.row == 0 else "")) == "Column"
        ]
        covered_columns = {
            column
            for item in column_headers
            for column in item.columns
        }
        if not column_headers or covered_columns != set(range(width)):
            existing_header_keys = {id(item.cell) for item in headers}
            headers.extend(
                item
                for item in grid
                if item.row == 0
                and id(item.cell) not in existing_header_keys
                and any(column not in covered_columns for column in item.columns)
            )
    unique_headers: dict[int, _GridCell] = {}
    for item in headers:
        unique_headers.setdefault(id(item.cell), item)
    headers = sorted(unique_headers.values(), key=lambda item: (item.row, item.col))

    for row_index, items in row_grid.items():
        for item in items:
            if _set_missing_parent(item.cell, rows[row_index]):
                changed[0] = True
            if _set_missing_parent(rows[row_index], table):
                changed[0] = True

    used_ids: set[str] = set()
    header_ids: dict[int, str] = {}
    sequence = 0
    for item in headers:
        desired_scope = "Row" if _scope(item.cell) == "Row" else "Column"
        _promote_to_th(
            item.cell,
            scope=desired_scope,
            conflicts=conflicts,
            changed=changed,
        )
        if item.cell.get("/S") != "/TH":
            unresolved.append(f"table{index}: cell at row {item.row + 1}, column {item.col + 1} is not a header")
            continue
        cell_id, sequence = _ensure_header(
            item.cell,
            index=index,
            sequence=sequence,
            used_ids=used_ids,
            existing_id_counts=existing_id_counts,
            conflicts=conflicts,
            changed=changed,
        )
        header_ids[id(item.cell)] = cell_id

    known_ids = set(existing_id_counts) | used_ids
    header_keys = {id(item.cell) for item in headers}
    for item in grid:
        if id(item.cell) in header_keys:
            continue
        _ensure_td(item.cell, changed)
        references: list[str] = []
        for header in headers:
            header_id = header_ids.get(id(header.cell))
            if not header_id:
                continue
            if (
                header.row < item.row
                and _scope(header.cell) != "Row"
                and any(
                    column in item.columns for column in header.columns
                )
            ):
                references.append(header_id)
            if (
                _scope(header.cell) == "Row"
                and header.row <= item.row <= header.last_row
            ):
                references.append(header_id)
        references = list(dict.fromkeys(references))
        _associate_headers(
            item.cell,
            references,
            label=f"table{index} row {item.row + 1} column {item.col + 1}",
            known_ids=known_ids,
            conflicts=conflicts,
            changed=changed,
        )
    if not header_ids:
        unresolved.append(f"table{index}: no usable /TH cells could be established")
    return changed[0]


def repair_layout_tables(pdf_bytes: bytes) -> tuple[bytes, LayoutTableRepairResult]:
    actions: list[str] = []
    conflicts: list[str] = []
    unresolved: list[str] = []
    unwrapped_1x1 = 0
    unwrapped_grid = 0
    annotated_data_tables = 0
    ambiguous_tables = 0
    changed = False

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if not isinstance(struct_root, pikepdf.Dictionary):
            return pdf_bytes, LayoutTableRepairResult(0, 0, 0, 0, [], [], [], 0)
        tables = [
            node for node in _iter_dict_nodes(struct_root) if node.get("/S") == "/Table"
        ]
        existing_id_counts = _all_existing_ids(tables)
        for index, table in enumerate(tables, start=1):
            rows, cells = _table_rows_and_cells(table)
            if _is_explicit_layout_table(table):
                if len(rows) == 1 and len(cells) == 1:
                    _unwrap_single_cell_table(table, rows, cells[0])
                    unwrapped_1x1 += 1
                    changed = True
                    actions.append(f"table{index}: unwrapped explicitly marked 1x1 layout to /Sect")
                elif cells:
                    _unwrap_grid_table(table, rows, cells)
                    unwrapped_grid += 1
                    changed = True
                    actions.append(f"table{index}: unwrapped explicitly marked layout to /Sect")
                else:
                    unresolved.append(f"table{index}: marked layout has no cells; retained")
                continue

            if _is_adobe_layout_table(table, rows, cells):
                _unwrap_grid_table(table, rows, cells)
                unwrapped_grid += 1
                changed = True
                actions.append(
                    f"table{index}: unwrapped Adobe auto-tagged layout to /Sect"
                )
                continue

            table_changed = [False]
            _ensure_summary(table, index, table_changed)
            before_conflicts = len(conflicts)
            compact_two_row_header = (
                len(rows) == 2
                and len(_row_direct_cells(rows[0])) > 0
                and len(_row_direct_cells(rows[1])) > len(_row_direct_cells(rows[0]))
            )
            was_changed = _annotate_data_table(
                table,
                rows,
                index,
                existing_id_counts=existing_id_counts,
                conflicts=conflicts,
                unresolved=unresolved,
            )
            logical_widths = _logical_row_widths(rows)
            differing_widths = (
                logical_widths is not None and len(set(logical_widths)) > 1
            )
            if len(rows) < 2 or not rows or not cells:
                ambiguous_tables += 1
                if not unresolved or not unresolved[-1].startswith(f"table{index}:"):
                    unresolved.append(f"table{index}: ambiguous structure retained as /Table")
                if table_changed[0]:
                    changed = True
                    actions.append(f"table{index}: retained ambiguous /Table with /Summary")
                continue
            if differing_widths:
                ambiguous_tables += 1
            if was_changed:
                changed = True
                if differing_widths:
                    actions.append(
                        f"table{index}: retained ambiguous /Table with differing logical row widths"
                    )
                else:
                    annotated_data_tables += 1
                if compact_two_row_header and not differing_widths:
                    actions.append(f"table{index}: annotated two-row header table")
                elif not differing_widths:
                    actions.append(f"table{index}: normalized logical grid headers and associations")
            elif len(conflicts) > before_conflicts:
                actions.append(f"table{index}: retained existing metadata and reported conflicts")
            if table_changed[0] and not was_changed:
                changed = True
                actions.append(f"table{index}: added missing /Summary")

        result = LayoutTableRepairResult(
            tables_found=len(tables),
            unwrapped_1x1=unwrapped_1x1,
            unwrapped_grid=unwrapped_grid,
            annotated_data_tables=annotated_data_tables,
            actions=actions,
            conflicts=list(dict.fromkeys(conflicts)),
            unresolved=list(dict.fromkeys(unresolved)),
            ambiguous_tables=ambiguous_tables,
        )
        if not changed:
            return pdf_bytes, result
        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue(), result
