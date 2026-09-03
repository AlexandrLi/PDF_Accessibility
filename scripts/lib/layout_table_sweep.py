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
    if isinstance(kids, pikepdf.Dictionary):
        return (
            [kids]
            if kids.get("/S") in ("/TD", "/TH", "/Span")
            else []
        )
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


def _has_exact_span(
    cell: pikepdf.Dictionary,
    key: str,
    expected: int,
) -> bool:
    values = list(_attribute_values(cell, key))
    if not values:
        return expected == 1
    parsed = [_as_positive_int(value) for value in values]
    return (
        all(value is not None for value in parsed)
        and len(set(parsed)) == 1
        and parsed[0] == expected
    )


def _is_empty_cell(cell: pikepdf.Dictionary) -> bool:
    kids = cell.get("/K")
    return kids is None or isinstance(kids, pikepdf.Array) and len(kids) == 0


def _strict_header_references(cell: pikepdf.Dictionary) -> list[str] | None:
    value = cell.get("/Headers")
    if value is None:
        return []
    if not isinstance(value, pikepdf.Array):
        return None
    references = [str(item).strip() for item in value]
    if any(not reference for reference in references):
        return None
    if len(set(references)) != len(references):
        return None
    return references


def _neptune_process_present(table: pikepdf.Dictionary) -> bool:
    processes = {
        str(value).lstrip("/")
        for value in _owner_attribute_values(table, "/ADBE_TableProcess", "/ADBE_Table")
    }
    return "Neptune" in processes


def _word_row_cells_and_border(
    row: pikepdf.Dictionary,
) -> tuple[list[pikepdf.Dictionary], pikepdf.Dictionary] | None:
    kids = row.get("/K")
    if not isinstance(kids, pikepdf.Array) or len(kids) < 2:
        return None
    children = list(kids)
    if not all(isinstance(child, pikepdf.Dictionary) for child in children):
        return None
    cells = children[:-1]
    border = children[-1]
    if (
        not cells
        or any(cell.get("/S") != "/TD" for cell in cells)
        or border.get("/S") != "/Span"
        or not isinstance(border.get("/K"), Integral)
        or any(
            key not in {"/Type", "/S", "/K", "/Pg", "/P"}
            for key in border
        )
    ):
        return None
    return cells, border


def _has_table_semantics(obj: pikepdf.Dictionary) -> bool:
    return any(
        obj.get(key) is not None
        for key in (
            "/A",
            "/Attributes",
            "/C",
            "/Headers",
            "/Scope",
            "/RowSpan",
            "/ColSpan",
            "/Alt",
            "/ActualText",
        )
    )


def _is_word_layout_wrapper(
    table: pikepdf.Dictionary,
    rows: list[pikepdf.Dictionary],
) -> bool:
    if not rows or len(rows) > 2 or _has_table_semantics(table):
        return False
    row_parts = [_word_row_cells_and_border(row) for row in rows]
    if any(parts is None for parts in row_parts):
        return False
    cell_rows = [parts[0] for parts in row_parts if parts is not None]
    if any(_has_table_semantics(cell) for cells in cell_rows for cell in cells):
        return False
    if sum(
        _descendant_mcid_count(cell)
        for cells in cell_rows
        for cell in cells
    ) < 20:
        return False
    if len(cell_rows) == 1:
        return len(cell_rows[0]) in {2, 3}
    return [len(cells) for cells in cell_rows] == [2, 1]


def _is_word_callout_layout_box(
    table: pikepdf.Dictionary,
    rows: list[pikepdf.Dictionary],
) -> bool:
    """Recognize Word's bordered callout/fill-in boxes tagged as tables.

    A single /TR of plain /TD cells cannot express a header/data
    relationship, and a short stack whose rows disagree on width with at
    most one row wider than a single cell has no column correspondence at
    all (banner/content/banner boxes), so no /TH promotion can ever satisfy
    the headers rule; unwrapping the layout box is the only sound repair.
    """
    if not rows or len(rows) > 3 or _has_table_semantics(table):
        return False
    row_parts = [_word_row_cells_and_border(row) for row in rows]
    if any(parts is None for parts in row_parts):
        return False
    cell_rows = [parts[0] for parts in row_parts if parts is not None]
    if any(_has_table_semantics(cell) for cells in cell_rows for cell in cells):
        return False
    if len(cell_rows) == 1:
        return True
    widths = [len(cells) for cells in cell_rows]
    if len(set(widths)) == 1:
        return False
    return min(widths) == 1 and sum(1 for width in widths if width > 1) <= 1


def _descendant_mcid_count(obj: pikepdf.Object) -> int:
    if isinstance(obj, Integral):
        return 1
    if not isinstance(obj, pikepdf.Dictionary):
        return 0
    count = 1 if obj.get("/MCID") is not None else 0
    kids = obj.get("/K")
    values = list(kids) if isinstance(kids, pikepdf.Array) else [kids]
    return count + sum(
        _descendant_mcid_count(value)
        for value in values
        if value is not None
    )


def _prepare_word_grouped_header_table(
    table: pikepdf.Dictionary,
    rows: list[pikepdf.Dictionary],
    *,
    conflicts: list[str],
) -> bool:
    if len(rows) < 4 or _has_table_semantics(table):
        return False
    row_parts = [_word_row_cells_and_border(row) for row in rows]
    if any(parts is None for parts in row_parts):
        return False
    cell_rows = [parts[0] for parts in row_parts if parts is not None]
    if any(_has_table_semantics(cell) for cells in cell_rows for cell in cells):
        return False

    header_width = len(cell_rows[0])
    body_widths = [len(cells) for cells in cell_rows[1:]]
    if (
        header_width < 3
        or len(set(body_widths)) != 1
        or body_widths[0] <= header_width
        or (body_widths[0] - 1) % (header_width - 1)
    ):
        return False
    group_span = (body_widths[0] - 1) // (header_width - 1)
    if group_span < 2:
        return False
    header_mcid_counts = [
        _descendant_mcid_count(cell)
        for cell in cell_rows[0]
    ]
    if header_mcid_counts[0] != 1 or any(
        count <= 1 for count in header_mcid_counts[1:]
    ):
        return False

    for row, (cells, border) in zip(
        rows,
        (parts for parts in row_parts if parts is not None),
    ):
        last_cell = cells[-1]
        last_kid = last_cell.get("/K")
        if isinstance(last_kid, pikepdf.Array):
            last_cell["/K"] = pikepdf.Array([*last_kid, border])
        elif last_kid is None:
            last_cell["/K"] = pikepdf.Array([border])
        else:
            last_cell["/K"] = pikepdf.Array([last_kid, border])
        last_objgen = getattr(last_cell, "objgen", None)
        if isinstance(last_objgen, tuple) and last_objgen != (0, 0):
            border["/P"] = last_cell
        row["/K"] = pikepdf.Array(cells)

    first_row = cell_rows[0]
    for cell_index, cell in enumerate(first_row):
        desired_span = 1 if cell_index == 0 else group_span
        if desired_span > 1:
            cell["/A"] = pikepdf.Dictionary(
                {
                    "/O": pikepdf.Name("/Table"),
                    "/ColSpan": desired_span,
                }
            )
        _promote_to_th(
            cell,
            scope="Column",
            conflicts=conflicts,
            changed=[False],
        )
    for cell in cell_rows[1]:
        _promote_to_th(
            cell,
            scope="Column",
            conflicts=conflicts,
            changed=[False],
        )
    return True


def _canonicalize_neptune_placeholders(
    table: pikepdf.Dictionary,
    rows: list[pikepdf.Dictionary],
    *,
    index: int,
    unresolved: list[str],
) -> bool:
    """Remove only proven empty header placeholders from one Neptune pattern."""
    if not _neptune_process_present(table) or len(rows) != 2:
        return False

    first_row = _row_direct_cells(rows[0])
    data_row = _row_direct_cells(rows[1])
    if len(first_row) != 4 or len(data_row) != 4:
        return False
    if any(cell.get("/S") != "/TH" for cell in first_row):
        return False
    if not (
        _is_empty_cell(first_row[1])
        and _is_empty_cell(first_row[3])
        and not _is_empty_cell(first_row[0])
        and not _is_empty_cell(first_row[2])
    ):
        return False

    declared_columns = _single_nonnegative_attribute(
        list(_owner_attribute_values(table, "/ADBE_NumCol", "/Table"))
    )
    if declared_columns != 4:
        return False

    proof_failed = False
    declared_rows = _single_nonnegative_attribute(
        list(_owner_attribute_values(table, "/ADBE_NumRow", "/Table"))
    )
    if declared_rows is not None and declared_rows != len(rows):
        proof_failed = True

    real_headers = (first_row[0], first_row[2])
    expected_indices = (0, 2)
    for cell, expected_index in zip(real_headers, expected_indices):
        if not _has_exact_span(cell, "/ColSpan", 2) or not _has_exact_span(
            cell, "/RowSpan", 1
        ):
            proof_failed = True
        values = list(_owner_attribute_values(cell, "/ADBE_ColIndex", "/Table"))
        if _single_nonnegative_attribute(values) != expected_index:
            proof_failed = True

    real_indices = [
        _single_nonnegative_attribute(
            list(_owner_attribute_values(cell, "/ADBE_ColIndex", "/Table"))
        )
        for cell in real_headers
    ]
    if (
        any(index_value is None for index_value in real_indices)
        or len(set(real_indices)) != len(real_indices)
        or any(
            index_value < 0 or index_value + 2 > declared_columns
            for index_value in real_indices
            if index_value is not None
        )
        or {
            column
            for index_value in real_indices
            if index_value is not None
            for column in range(index_value, index_value + 2)
        }
        != set(range(declared_columns))
    ):
        proof_failed = True

    placeholder_indices: list[int] = []
    placeholder_ids: list[str] = []
    for placeholder in (first_row[1], first_row[3]):
        if not _has_exact_span(placeholder, "/ColSpan", 1) or not _has_exact_span(
            placeholder, "/RowSpan", 1
        ):
            proof_failed = True
        placeholder_id = str(placeholder.get("/ID") or "").strip()
        if not placeholder_id or str(placeholder.get("/Scope") or "").lstrip(
            "/"
        ) != "Column":
            proof_failed = True
        else:
            placeholder_ids.append(placeholder_id)
        if any(
            key not in {"/Type", "/S", "/K", "/Pg", "/P", "/ID", "/Scope"}
            for key in placeholder
        ):
            proof_failed = True
        values = list(_owner_attribute_values(placeholder, "/ADBE_ColIndex", "/Table"))
        if values:
            placeholder_index = _single_nonnegative_attribute(values)
            if placeholder_index is None:
                proof_failed = True
            else:
                placeholder_indices.append(placeholder_index)

    if len(set(placeholder_indices)) != len(placeholder_indices) or any(
        placeholder_index not in set(range(declared_columns))
        for placeholder_index in placeholder_indices
    ):
        proof_failed = True
    if any(
        placeholder_index in set(expected_indices)
        for placeholder_index in placeholder_indices
    ):
        proof_failed = True

    if any(
        not _has_exact_span(cell, "/ColSpan", 1)
        or not _has_exact_span(cell, "/RowSpan", 1)
        for cell in data_row
    ):
        proof_failed = True

    if len(set(placeholder_ids)) != len(placeholder_ids):
        proof_failed = True

    placeholder_replacements = dict(zip(placeholder_ids, ("", "")))
    if len(placeholder_replacements) == 2:
        real_ids = [
            str(cell.get("/ID") or "").strip()
            for cell in real_headers
        ]
        if any(not cell_id for cell_id in real_ids) or len(set(real_ids)) != 2:
            proof_failed = True
        else:
            placeholder_replacements = dict(
                zip(placeholder_ids, real_ids)
            )
            planned_headers: dict[int, list[str]] = {}
            all_cells = _table_rows_and_cells(table)[1]
            data_cell_indices = {
                getattr(cell, "objgen", None): data_index
                for data_index, cell in enumerate(data_row)
                if isinstance(getattr(cell, "objgen", None), tuple)
                and getattr(cell, "objgen", None) != (0, 0)
            }
            for cell in all_cells:
                raw_headers = cell.get("/Headers")
                if raw_headers is None:
                    continue
                references = _strict_header_references(cell)
                if references is None:
                    proof_failed = True
                    continue
                for reference in references:
                    if reference not in placeholder_replacements:
                        continue
                    matching_data_index = data_cell_indices.get(
                        getattr(cell, "objgen", None)
                    )
                    if matching_data_index is None:
                        matching_data_index = next(
                            (
                                data_index
                                for data_index, data_cell in enumerate(data_row)
                                if data_cell is cell
                            ),
                            None,
                        )
                    expected_placeholder = (
                        placeholder_ids[0]
                        if matching_data_index == 1
                        else placeholder_ids[1]
                        if matching_data_index == 3
                        else None
                    )
                    if reference != expected_placeholder:
                        proof_failed = True

            for data_index, cell in enumerate(data_row):
                references = _strict_header_references(cell)
                if references is None:
                    proof_failed = True
                    continue
                expected_placeholder = (
                    placeholder_ids[0]
                    if data_index == 1
                    else placeholder_ids[1]
                    if data_index == 3
                    else None
                )
                if expected_placeholder is not None and expected_placeholder not in references:
                    proof_failed = True
                if expected_placeholder is None and any(
                    reference in placeholder_replacements
                    for reference in references
                ):
                    proof_failed = True
                rewritten = [
                    placeholder_replacements.get(reference, reference)
                    for reference in references
                ]
                if len(set(rewritten)) != len(rewritten):
                    proof_failed = True
                planned_headers[id(cell)] = rewritten
    else:
        planned_headers = {}

    if proof_failed:
        unresolved.append(
            f"table{index}: Neptune placeholder canonicalization proof incomplete or conflicting; retained"
        )
        return False

    canonical_rows = [
        [first_row[0], first_row[2]],
        data_row,
    ]
    logical_widths = _logical_row_widths(
        [
            pikepdf.Dictionary({"/K": pikepdf.Array(cells)})
            for cells in canonical_rows
        ]
    )
    if logical_widths != [declared_columns, declared_columns]:
        unresolved.append(
            f"table{index}: Neptune placeholder removal did not produce one regular grid; retained"
        )
        return False

    rows[0]["/K"] = pikepdf.Array(canonical_rows[0])
    for cell in data_row:
        references = planned_headers.get(id(cell))
        if references is not None:
            cell["/Headers"] = pikepdf.Array(
                [pikepdf.String(reference) for reference in references]
            )
    return True


def _is_adobe_autotagged_table(table: pikepdf.Dictionary) -> bool:
    """True when the table's attributes prove Adobe Auto-Tag produced it."""
    for name in ("/ADBE_NumCol", "/ADBE_NumRow"):
        if list(_owner_attribute_values(table, name, "/Table")):
            return True
    return bool(
        list(_owner_attribute_values(table, "/ADBE_TableProcess", "/ADBE_Table"))
    )


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


def _is_pdf_lib_overlapping_layout_table(
    rows: list[pikepdf.Dictionary],
) -> bool:
    if len(rows) != 3 or _logical_row_widths(rows) != [2, 4, 4]:
        return False
    cell_rows = [_row_direct_cells(row) for row in rows]
    if [len(cells) for cells in cell_rows] != [2, 2, 2]:
        return False
    if [
        [str(cell.get("/S")) for cell in cells]
        for cells in cell_rows
    ] != [["/TH", "/TH"], ["/TD", "/TD"], ["/TH", "/TD"]]:
        return False

    top_row_spans = [
        _span(cell, "/RowSpan", conflicts=[], label="pdf-lib top header")
        for cell in cell_rows[0]
    ]
    if (
        len(set(top_row_spans)) != 1
        or top_row_spans[0] < len(rows)
        or any(
            _span(cell, "/RowSpan", conflicts=[], label="pdf-lib body cell") != 1
            for cells in cell_rows[1:]
            for cell in cells
        )
        or any(
            _span(cell, "/ColSpan", conflicts=[], label="pdf-lib cell") != 1
            for cells in cell_rows
            for cell in cells
        )
    ):
        return False

    column_indices: list[list[int]] = []
    for cells in cell_rows:
        row_indices: list[int] = []
        for cell in cells:
            index = _single_nonnegative_attribute(
                list(_owner_attribute_values(cell, "/ADBE_ColIndex", "/Table"))
            )
            if index is None or not list(
                _owner_attribute_values(
                    cell,
                    "/ADBE_Confidence",
                    "/ADBE_Table",
                )
            ):
                return False
            row_indices.append(index)
        column_indices.append(row_indices)
    return column_indices == [[0, 1], [1, 0], [0, 1]]


def _remove_key(obj: pikepdf.Dictionary, key: str) -> None:
    if key in obj:
        del obj[key]


def _set_owner_attribute(
    obj: pikepdf.Dictionary,
    *,
    owner: str,
    key: str,
    value: object | None,
) -> bool:
    attributes = obj.get("/A")
    items = list(attributes) if isinstance(attributes, pikepdf.Array) else [attributes]
    owner_items = [
        item
        for item in items
        if isinstance(item, pikepdf.Dictionary) and item.get("/O") == owner
    ]
    changed = False
    if not owner_items:
        attribute = pikepdf.Dictionary({"/O": pikepdf.Name(owner)})
        if value is not None:
            attribute[key] = value
        if attributes is None:
            obj["/A"] = attribute
        elif isinstance(attributes, pikepdf.Array):
            attributes.append(attribute)
        else:
            obj["/A"] = pikepdf.Array([attributes, attribute])
        return True

    for attribute in owner_items:
        existing = attribute.get(key)
        if value is None:
            if existing is not None:
                del attribute[key]
                changed = True
        elif existing != value:
            attribute[key] = value
            changed = True
    return changed


_TM_X_PATTERN = re.compile(
    rb"(?:[-\d.]+\s+){4}([-\d.]+)\s+[-\d.]+\s+Tm\b"
)


def _page_contents_bytes(page: object) -> bytes | None:
    if not isinstance(page, pikepdf.Dictionary):
        return None
    contents = page.get("/Contents")
    if isinstance(contents, pikepdf.Array):
        parts = []
        for item in contents:
            try:
                parts.append(item.read_bytes())
            except Exception:
                return None
        return b"".join(parts)
    try:
        return contents.read_bytes()
    except Exception:
        return None


def _cell_mcids(cell: pikepdf.Dictionary) -> list[int]:
    mcids: list[int] = []

    def walk(node: object) -> None:
        if isinstance(node, pikepdf.Dictionary):
            if node.get("/MCID") is not None and node.get("/S") is None:
                try:
                    mcids.append(int(node.get("/MCID")))
                except (TypeError, ValueError):
                    pass
                return
            kids = node.get("/K")
            values = list(kids) if isinstance(kids, pikepdf.Array) else [kids]
            for value in values:
                walk(value)
        elif isinstance(node, Integral):
            mcids.append(int(node))

    walk(cell)
    return mcids


def _cell_first_text_x(cell: pikepdf.Dictionary, data: bytes) -> float | None:
    from lib.marked_content_actualtext_sweep import _get_mcid_block

    for mcid in _cell_mcids(cell):
        block = _get_mcid_block(data, mcid)
        if block is None:
            continue
        match = _TM_X_PATTERN.search(block[1])
        if match is None:
            continue
        try:
            return float(match.group(1))
        except ValueError:
            continue
    return None


_COLUMN_X_TOLERANCE = 3.0


def _reconcile_geometric_header_spans(
    table: pikepdf.Dictionary,
    rows: list[pikepdf.Dictionary],
    *,
    index: int,
    actions: list[str],
) -> bool:
    """Give a short first header row /ColSpan values proven by text geometry.

    Word tags grouped-header tables with a first row of fewer cells than the
    body without recording the spans. When every header cell's first text
    x-position aligns with a distinct body column start, the spans between
    those columns are the producer's own layout evidence.
    """
    if _has_table_semantics(table) or len(rows) < 3:
        return False
    row_parts = [_word_row_cells_and_border(row) for row in rows]
    if any(parts is None for parts in row_parts):
        return False
    cell_rows = [parts[0] for parts in row_parts if parts is not None]
    if any(_has_table_semantics(cell) for cells in cell_rows for cell in cells):
        return False
    header = cell_rows[0]
    body_rows = cell_rows[1:]
    body_widths = {len(cells) for cells in body_rows}
    if len(body_widths) != 1:
        return False
    width = body_widths.pop()
    if not (1 < len(header) < width):
        return False

    page = rows[0].get("/Pg") or table.get("/Pg")
    data = _page_contents_bytes(page)
    if data is None:
        return False

    column_xs: list[list[float]] = [[] for _ in range(width)]
    for cells in body_rows:
        for position, cell in enumerate(cells):
            x = _cell_first_text_x(cell, data)
            if x is not None:
                column_xs[position].append(x)
    if any(len(xs) < 2 for xs in column_xs):
        return False
    # Left-aligned text starts at the column's left edge; right-aligned or
    # centered values start further in, so the per-column minimum is the
    # only stable estimate of where each column begins.
    columns = [min(xs) for xs in column_xs]
    if any(
        later - earlier <= _COLUMN_X_TOLERANCE
        for earlier, later in zip(columns, columns[1:])
    ):
        return False

    header_columns: list[int] = []
    for cell in header:
        x = _cell_first_text_x(cell, data)
        if x is None:
            return False
        # A header starts inside the leftmost column of its span (it may be
        # centered across the span, so it need not start at the edge).
        boundary = None
        for position, column_x in enumerate(columns):
            if column_x <= x + _COLUMN_X_TOLERANCE:
                boundary = position
        if boundary is None:
            return False
        header_columns.append(boundary)
    if header_columns[0] != 0 or sorted(set(header_columns)) != header_columns:
        return False

    boundaries = header_columns + [width]
    spans = [
        boundaries[position + 1] - boundaries[position]
        for position in range(len(header))
    ]
    if any(span < 1 for span in spans) or all(span == 1 for span in spans):
        return False

    for row, parts in zip(rows, row_parts):
        cells, border = parts  # type: ignore[misc]
        last_cell = cells[-1]
        last_kid = last_cell.get("/K")
        if isinstance(last_kid, pikepdf.Array):
            last_cell["/K"] = pikepdf.Array([*last_kid, border])
        elif last_kid is None:
            last_cell["/K"] = pikepdf.Array([border])
        else:
            last_cell["/K"] = pikepdf.Array([last_kid, border])
        last_objgen = getattr(last_cell, "objgen", None)
        if isinstance(last_objgen, tuple) and last_objgen != (0, 0):
            border["/P"] = last_cell
        row["/K"] = pikepdf.Array(cells)

    for cell, span in zip(header, spans):
        if span > 1:
            cell["/A"] = pikepdf.Dictionary(
                {
                    "/O": pikepdf.Name("/Table"),
                    "/ColSpan": span,
                }
            )
    actions.append(
        f"table{index}: derived header column spans {spans} from text geometry"
    )
    return True


def _reconcile_declared_column_spans(
    table: pikepdf.Dictionary,
    rows: list[pikepdf.Dictionary],
) -> bool:
    declared_columns = _single_nonnegative_attribute(
        list(_owner_attribute_values(table, "/ADBE_NumCol", "/Table"))
    )
    if declared_columns is None or declared_columns < 1:
        return False

    row_cells: list[tuple[list[pikepdf.Dictionary], list[int]]] = []
    for row in rows:
        cells = _row_direct_cells(row)
        indices = [
            _single_nonnegative_attribute(
                list(_owner_attribute_values(cell, "/ADBE_ColIndex", "/Table"))
            )
            for cell in cells
        ]
        if (
            not cells
            or any(index is None for index in indices)
            or indices[0] != 0
            or indices != sorted(set(indices))
            or indices[-1] >= declared_columns
        ):
            return False
        row_cells.append((cells, [index for index in indices if index is not None]))

    changed = False
    for cells, indices in row_cells:
        boundaries = [*indices[1:], declared_columns]
        for cell, start, end in zip(cells, indices, boundaries):
            desired_span = end - start
            if desired_span < 1:
                return False
            if "/ColSpan" in cell:
                del cell["/ColSpan"]
                changed = True
            changed |= _set_owner_attribute(
                cell,
                owner="/Table",
                key="/ColSpan",
                value=desired_span if desired_span > 1 else None,
            )
    return changed


def _ensure_summary(
    table: pikepdf.Dictionary,
    index: int,
    changed: list[bool],
) -> None:
    direct_summary = str(table.get("/Summary") or "").strip()
    attribute_summaries = [
        str(value).strip()
        for value in _owner_attribute_values(table, "/Summary", "/Table")
        if str(value).strip()
    ]
    summary = attribute_summaries[0] if attribute_summaries else direct_summary
    if not summary:
        summary = f"Table {index}"
    if _set_owner_attribute(
        table,
        owner="/Table",
        key="/Summary",
        value=pikepdf.String(summary),
    ):
        changed[0] = True
    if "/Summary" in table:
        del table["/Summary"]
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
        producer = str(pdf.docinfo.get("/Producer") or "")
        is_word_2010 = producer == "Microsoft® Word 2010"
        is_pdf_lib = producer.startswith("pdf-lib ")
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

            if is_word_2010 and _is_word_layout_wrapper(table, rows):
                _unwrap_grid_table(table, rows, cells)
                unwrapped_grid += 1
                changed = True
                actions.append(
                    f"table{index}: unwrapped proven Word comparison layout to /Sect"
                )
                continue

            if is_word_2010 and _is_word_callout_layout_box(table, rows):
                direct_cells = [
                    cell
                    for row in rows
                    for cell in _row_direct_cells(row)
                    if cell.get("/S") == "/TD"
                ]
                if len(rows) == 1 and len(direct_cells) == 1:
                    _unwrap_single_cell_table(table, rows, direct_cells[0])
                    unwrapped_1x1 += 1
                else:
                    _unwrap_grid_table(table, rows, cells)
                    unwrapped_grid += 1
                changed = True
                actions.append(
                    f"table{index}: unwrapped Word callout layout box to /Sect"
                )
                continue

            if is_pdf_lib and _is_pdf_lib_overlapping_layout_table(rows):
                _unwrap_grid_table(table, rows, cells)
                unwrapped_grid += 1
                changed = True
                actions.append(
                    f"table{index}: unwrapped overlapping pdf-lib layout to /Sect"
                )
                continue

            if _is_adobe_layout_table(table, rows, cells):
                _unwrap_grid_table(table, rows, cells)
                unwrapped_grid += 1
                changed = True
                actions.append(
                    f"table{index}: unwrapped Adobe auto-tagged layout to /Sect"
                )
                continue

            if _canonicalize_neptune_placeholders(
                table,
                rows,
                index=index,
                unresolved=unresolved,
            ):
                changed = True
                actions.append(
                    f"table{index}: removed proven Neptune empty header placeholders"
                )
                rows, cells = _table_rows_and_cells(table)

            if is_word_2010 and _prepare_word_grouped_header_table(
                table,
                rows,
                conflicts=conflicts,
            ):
                changed = True
                actions.append(
                    f"table{index}: normalized Word grouped headers and border spans"
                )
                rows, cells = _table_rows_and_cells(table)

            if is_word_2010 and _reconcile_geometric_header_spans(
                table,
                rows,
                index=index,
                actions=actions,
            ):
                changed = True
                rows, cells = _table_rows_and_cells(table)

            table_changed = [False]
            if _reconcile_declared_column_spans(table, rows):
                table_changed[0] = True
                actions.append(
                    f"table{index}: reconciled spans with declared Adobe column indices"
                )
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
                    actions.append(
                        f"table{index}: retained ambiguous /Table with normalized /Summary"
                    )
                continue
            if differing_widths and _is_adobe_autotagged_table(table):
                # An Auto-Tag table is a heuristic guess, not author
                # intent; one still irregular after every grid repair can
                # only fail the checker, so read it as layout instead.
                rows, cells = _table_rows_and_cells(table)
                _unwrap_grid_table(table, rows, cells)
                unwrapped_grid += 1
                changed = True
                actions.append(
                    f"table{index}: unwrapped irregular Adobe auto-tagged table to /Sect"
                )
                note = (
                    f"table{index}: differing logical row widths; "
                    "retained as ambiguous /Table"
                )
                if note in unresolved:
                    unresolved.remove(note)
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
                actions.append(f"table{index}: normalized /Summary attribute")

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
