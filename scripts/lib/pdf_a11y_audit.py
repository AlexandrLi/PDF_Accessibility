"""Lightweight PDF/UA structure checks on remediated preview bytes."""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass, field
from numbers import Integral

import pikepdf

from lib.figure_alt_quality import (
    SuspiciousFigureAlt,
    classify_figure_alt,
    speaks_for_content,
    struct_class_names,
)
from lib.marked_content_actualtext_sweep import (
    count_orphan_marked_missing_actualtext,
    list_nested_alternate_text,
    list_untagged_image_mcids_missing_actualtext,
)
from lib.tagged_content_sweep import collect_tagged_content_diagnostics


@dataclass
class PdfA11yAudit:
    marked: bool
    figure_count: int
    figures_missing_alt: list[int]
    figures_suspicious_alt: list[SuspiciousFigureAlt]
    table_count: int
    tables_without_summary: int
    tables_without_th: int
    tables_th_missing_scope: list[str] = field(default_factory=list)
    tables_th_invalid_scope: list[str] = field(default_factory=list)
    tables_th_missing_id: list[str] = field(default_factory=list)
    tables_th_duplicate_id: list[str] = field(default_factory=list)
    data_cells_missing_headers: list[str] = field(default_factory=list)
    data_cells_with_explicit_headers: list[str] = field(default_factory=list)
    data_cells_malformed_headers: list[str] = field(default_factory=list)
    data_cells_unresolved_headers: list[str] = field(default_factory=list)
    invalid_row_child_roles: list[str] = field(default_factory=list)
    tables_with_inconsistent_row_widths: list[str] = field(default_factory=list)
    logical_row_widths: dict[str, list[int]] = field(default_factory=dict)
    struct_tree_root_present: bool = False
    parent_tree_present: bool = False
    pages_with_struct_parents: int = 0
    pages_with_mapped_mcids: int = 0
    mapped_mcid_count: int = 0
    unresolved_mcids: list[str] = field(default_factory=list)
    parent_tree_keys: list[int] = field(default_factory=list)
    nonfigure_image_mcids_missing_alt: list[str] = field(default_factory=list)
    orphan_marked_mcids_missing_actualtext: int = 0
    nested_alternate_text: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["figures_suspicious_alt"] = [
            item.to_dict() for item in self.figures_suspicious_alt
        ]
        return payload

    @property
    def has_blocking_issues(self) -> bool:
        return bool(self.figures_missing_alt or self.figures_suspicious_alt)

    @property
    def is_likely_remediated(self) -> bool:
        return self.marked and not self.has_blocking_issues


def is_likely_remediated(pdf_bytes: bytes) -> bool:
    return audit_pdf_bytes(pdf_bytes).is_likely_remediated


def audit_pdf_bytes(pdf_bytes: bytes) -> PdfA11yAudit:
    figures_missing_alt: list[int] = []
    figures_suspicious_alt: list[SuspiciousFigureAlt] = []
    figure_index = 0
    table_count = 0
    tables_without_summary = 0
    tables_without_th = 0
    tables_th_missing_scope: list[str] = []
    tables_th_invalid_scope: list[str] = []
    tables_th_missing_id: list[str] = []
    tables_th_duplicate_id: list[str] = []
    th_ids: list[tuple[str, str]] = []
    data_cells_missing_headers: list[str] = []
    data_cells_with_explicit_headers: list[str] = []
    data_cells_malformed_headers: list[str] = []
    data_cells_unresolved_headers: list[str] = []
    data_cells_for_headers: list[tuple[str, object]] = []
    invalid_row_child_roles: list[str] = []
    tables_with_inconsistent_row_widths: list[str] = []
    logical_row_widths: dict[str, list[int]] = {}

    def direct_kids(obj: pikepdf.Dictionary) -> list[pikepdf.Object]:
        kids = obj.get("/K")
        if isinstance(kids, pikepdf.Array):
            return list(kids)
        return [kids] if kids is not None else []

    def descendant_dicts(obj: pikepdf.Object) -> list[pikepdf.Dictionary]:
        if not isinstance(obj, pikepdf.Dictionary):
            return []
        descendants = [obj]
        for kid in direct_kids(obj):
            descendants.extend(descendant_dicts(kid))
        return descendants

    def pdf_string(value: object) -> str | None:
        if isinstance(value, pikepdf.String):
            return str(value).strip()
        if isinstance(value, str):
            return value.strip()
        return None

    def pdf_name(value: object) -> str | None:
        if isinstance(value, pikepdf.Name):
            return str(value).lstrip("/").strip()
        return None

    def owner_attribute_values(
        obj: pikepdf.Dictionary,
        key: str,
        owner: str,
    ) -> list[object]:
        values: list[object] = []
        for container_key in ("/A", "/Attributes"):
            attributes = obj.get(container_key)
            candidates = (
                list(attributes)
                if isinstance(attributes, pikepdf.Array)
                else [attributes]
            )
            for candidate in candidates:
                if (
                    isinstance(candidate, pikepdf.Dictionary)
                    and candidate.get("/O") == owner
                    and candidate.get(key) is not None
                ):
                    values.append(candidate[key])
        return values

    def span_value(cell: pikepdf.Dictionary, key: str) -> int | None:
        values: list[object] = []
        for container_key in (key, "/Attributes", "/A"):
            value = cell.get(container_key)
            candidates = list(value) if isinstance(value, pikepdf.Array) else [value]
            for candidate in candidates:
                if container_key == key and candidate is not None:
                    values.append(candidate)
                elif isinstance(candidate, pikepdf.Dictionary):
                    nested = candidate.get(key)
                    if nested is not None:
                        values.append(nested)
        if not values:
            return 1
        parsed: list[int] = []
        for value in values:
            if isinstance(value, Integral):
                number = int(value)
            else:
                try:
                    number = int(str(value))
                except (TypeError, ValueError):
                    return None
            if number < 1:
                return None
            parsed.append(number)
        return parsed[0] if len(set(parsed)) == 1 else None

    def derive_row_widths(
        rows: list[pikepdf.Dictionary],
    ) -> list[int] | None:
        if not rows:
            return None
        occupied: dict[int, int] = {}
        widths = [0] * len(rows)
        for row_index, row in enumerate(rows):
            cells = [
                kid
                for kid in direct_kids(row)
                if isinstance(kid, pikepdf.Dictionary)
                and kid.get("/S") in ("/TD", "/TH")
            ]
            if not cells or len(cells) != len(direct_kids(row)):
                return None
            column = 0
            for cell in cells:
                while occupied.get(column, -1) >= row_index:
                    column += 1
                col_span = span_value(cell, "/ColSpan")
                row_span = span_value(cell, "/RowSpan")
                if col_span is None or row_span is None:
                    return None
                while any(
                    occupied.get(candidate, -1) >= row_index
                    for candidate in range(column, column + col_span)
                ):
                    column += 1
                last_row = row_index + row_span - 1
                for candidate in range(column, column + col_span):
                    occupied[candidate] = max(occupied.get(candidate, -1), last_row)
                column += col_span
                widths[row_index] = max(widths[row_index], column)
        return widths

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        mark_info = pdf.Root.get("/MarkInfo")
        marked = bool(mark_info and mark_info.get("/Marked"))
        struct_root = pdf.Root.get("/StructTreeRoot")
        tagged_content = collect_tagged_content_diagnostics(pdf)

        def walk_figures(obj: pikepdf.Object, spoken_by_ancestor: bool = False) -> None:
            nonlocal figure_index
            if not isinstance(obj, pikepdf.Dictionary):
                return
            spoken = spoken_by_ancestor or speaks_for_content(obj)
            if obj.get("/S") == "/Figure":
                figure_index += 1
                alt = obj.get("/Alt")
                alt_text = str(alt).strip() if alt is not None else ""
                if not alt_text:
                    if not spoken_by_ancestor:
                        figures_missing_alt.append(figure_index)
                else:
                    reasons = classify_figure_alt(
                        alt_text,
                        struct_classes=struct_class_names(obj),
                    )
                    if reasons:
                        figures_suspicious_alt.append(
                            SuspiciousFigureAlt(
                                figure_index=figure_index,
                                alt_text=alt_text,
                                reasons=tuple(reasons),
                            )
                        )
            kids = obj.get("/K")
            if isinstance(kids, pikepdf.Array):
                for kid in kids:
                    if isinstance(kid, pikepdf.Dictionary):
                        walk_figures(kid, spoken)
                    elif isinstance(kid, pikepdf.Array):
                        for nested in kid:
                            if isinstance(nested, pikepdf.Dictionary):
                                walk_figures(nested, spoken)
            elif isinstance(kids, pikepdf.Dictionary):
                walk_figures(kids, spoken)

        def walk_tables(obj: pikepdf.Object) -> None:
            nonlocal table_count, tables_without_summary, tables_without_th
            if not isinstance(obj, pikepdf.Dictionary):
                return
            if obj.get("/S") == "/Table":
                table_count += 1
                table_label = f"table{table_count}"
                summaries = owner_attribute_values(obj, "/Summary", "/Table")
                if not any(str(summary).strip() for summary in summaries):
                    tables_without_summary += 1
                descendants = descendant_dicts(obj)
                th_nodes = [
                    node for node in descendants if node.get("/S") == "/TH"
                ]
                th_count = len(th_nodes)
                if th_count == 0:
                    tables_without_th += 1

                for th_index, cell in enumerate(th_nodes, start=1):
                    label = f"{table_label} TH {th_index}"
                    raw_scope = cell.get("/Scope")
                    scope = pdf_string(raw_scope) or pdf_name(raw_scope)
                    if scope is None:
                        if raw_scope is None:
                            tables_th_missing_scope.append(label)
                        else:
                            tables_th_invalid_scope.append(label)
                    elif scope.lstrip("/") not in {"Column", "Row"}:
                        tables_th_invalid_scope.append(label)
                    cell_id = pdf_string(cell.get("/ID"))
                    if cell_id is None:
                        tables_th_missing_id.append(label)
                    else:
                        th_ids.append((label, cell_id))

                rows = [
                    node
                    for node in descendants
                    if node.get("/S") == "/TR"
                ]
                # ISO 32000 allows a /Table to hold /TR directly or to group
                # rows under /THead, /TBody and /TFoot, plus one /Caption.
                # Adobe accepts both shapes; only the rows inside a row group
                # must be /TR.
                direct_table_kids = direct_kids(obj)
                for child_index, child in enumerate(direct_table_kids, start=1):
                    child_role = (
                        child.get("/S") if isinstance(child, pikepdf.Dictionary) else None
                    )
                    if child_role == "/TR" or child_role == "/Caption":
                        continue
                    if child_role in ("/THead", "/TBody", "/TFoot"):
                        for group_index, group_child in enumerate(
                            direct_kids(child), start=1
                        ):
                            if not isinstance(
                                group_child, pikepdf.Dictionary
                            ) or group_child.get("/S") != "/TR":
                                role = (
                                    str(group_child.get("/S"))
                                    if isinstance(group_child, pikepdf.Dictionary)
                                    else str(group_child)
                                )
                                invalid_row_child_roles.append(
                                    f"{table_label} direct child {child_index} "
                                    f"{str(child_role).lstrip('/')} child {group_index}: "
                                    f"{role} (expected /TR)"
                                )
                        continue
                    role = (
                        str(child_role)
                        if isinstance(child, pikepdf.Dictionary)
                        else str(child)
                    )
                    invalid_row_child_roles.append(
                        f"{table_label} direct child {child_index}: "
                        f"{role} (expected /TR, /THead, /TBody, /TFoot or /Caption)"
                    )

                for row_index, row in enumerate(rows, start=1):
                    direct_children = direct_kids(row)
                    for child_index, child in enumerate(direct_children, start=1):
                        if not isinstance(child, pikepdf.Dictionary) or child.get(
                            "/S"
                        ) not in ("/TD", "/TH"):
                            role = (
                                str(child.get("/S"))
                                if isinstance(child, pikepdf.Dictionary)
                                else str(child)
                            )
                            invalid_row_child_roles.append(
                                f"{table_label} row {row_index} child {child_index}: "
                                f"{role} (expected /TD or /TH)"
                            )

                    for cell_index, cell in enumerate(direct_children, start=1):
                        if not isinstance(cell, pikepdf.Dictionary) or cell.get(
                            "/S"
                        ) != "/TD":
                            continue
                        label = f"{table_label} row {row_index} TD {cell_index}"
                        headers = cell.get("/Headers")
                        data_cells_for_headers.append((label, headers))
                        if headers is None:
                            data_cells_missing_headers.append(label)
                            continue
                        data_cells_with_explicit_headers.append(label)
                        if not isinstance(headers, pikepdf.Array) or not headers:
                            data_cells_malformed_headers.append(label)
                            continue
                        malformed = False
                        for reference in headers:
                            if pdf_string(reference) is None or not pdf_string(
                                reference
                            ):
                                malformed = True
                                break
                        if malformed:
                            data_cells_malformed_headers.append(label)

                widths = derive_row_widths(rows)
                if widths is not None:
                    logical_row_widths[table_label] = widths
                    if len(set(widths)) > 1:
                        tables_with_inconsistent_row_widths.append(table_label)
            kids = obj.get("/K")
            if isinstance(kids, pikepdf.Array):
                for kid in kids:
                    if isinstance(kid, pikepdf.Dictionary):
                        walk_tables(kid)
            elif isinstance(kids, pikepdf.Dictionary):
                walk_tables(kids)

        if struct_root:
            walk_figures(struct_root)
            walk_tables(struct_root)

    th_id_counts: dict[str, int] = {}
    for _label, cell_id in th_ids:
        th_id_counts[cell_id] = th_id_counts.get(cell_id, 0) + 1
    for label, cell_id in th_ids:
        if th_id_counts[cell_id] > 1:
            tables_th_duplicate_id.append(f"{label}: {cell_id}")

    valid_header_ids = {
        cell_id for cell_id, count in th_id_counts.items() if count == 1
    }
    for label, headers in data_cells_for_headers:
        if not isinstance(headers, pikepdf.Array):
            continue
        for reference in headers:
            reference_text = pdf_string(reference)
            if reference_text is not None and reference_text not in valid_header_ids:
                data_cells_unresolved_headers.append(
                    f"{label}: {reference_text or str(reference)}"
                )

    return PdfA11yAudit(
        marked=marked,
        figure_count=figure_index,
        figures_missing_alt=figures_missing_alt,
        figures_suspicious_alt=figures_suspicious_alt,
        table_count=table_count,
        tables_without_summary=tables_without_summary,
        tables_without_th=tables_without_th,
        tables_th_missing_scope=tables_th_missing_scope,
        tables_th_invalid_scope=tables_th_invalid_scope,
        tables_th_missing_id=tables_th_missing_id,
        tables_th_duplicate_id=tables_th_duplicate_id,
        data_cells_missing_headers=data_cells_missing_headers,
        data_cells_with_explicit_headers=data_cells_with_explicit_headers,
        data_cells_malformed_headers=data_cells_malformed_headers,
        data_cells_unresolved_headers=data_cells_unresolved_headers,
        invalid_row_child_roles=invalid_row_child_roles,
        tables_with_inconsistent_row_widths=tables_with_inconsistent_row_widths,
        logical_row_widths=logical_row_widths,
        struct_tree_root_present=tagged_content.struct_tree_root_present,
        parent_tree_present=tagged_content.parent_tree_present,
        pages_with_struct_parents=tagged_content.pages_with_struct_parents,
        pages_with_mapped_mcids=tagged_content.pages_with_mapped_mcids,
        mapped_mcid_count=tagged_content.mapped_mcid_count,
        unresolved_mcids=tagged_content.unresolved_mcids,
        parent_tree_keys=tagged_content.parent_tree_keys,
        nonfigure_image_mcids_missing_alt=(
            list_untagged_image_mcids_missing_actualtext(pdf_bytes)
        ),
        orphan_marked_mcids_missing_actualtext=(
            count_orphan_marked_missing_actualtext(pdf_bytes)
        ),
        nested_alternate_text=list_nested_alternate_text(pdf_bytes),
    )
