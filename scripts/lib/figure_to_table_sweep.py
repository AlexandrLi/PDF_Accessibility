"""Retag /Figure elements that describe tables as proper /Table structure."""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass

import pikepdf

from lib.figure_alt_quality import classify_figure_alt, struct_class_names


@dataclass
class FigureToTableRepairResult:
    figures_found: int
    converted: int
    actions: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


_COLUMN_HEADER_PATTERN = re.compile(
    r"columns?(?: are)?(?: labeled| labelled| include|:\s*)"
    r"([\s\S]+?)(?:\.\s*(?:rows?|the first row|another row)|\.\s*$)",
    re.IGNORECASE,
)

_QUOTED_CELL_PATTERN = re.compile(r"'([^']+)'")

_ROW_GLUT_PATTERN = re.compile(
    r"(?:another row includes|rows include|(?:the )?(?:first|second|third|fourth|fifth) row contains)\s+"
    r"'?(GLUT\d+)'?\s+with tissue expression in\s+"
    r"(.+?)\s+and biological role of\s+(.+?)(?=\.\s*(?:another row|$)|\.$|$)",
    re.IGNORECASE | re.DOTALL,
)

_ROW_CONTAINS_UNDER_PATTERN = re.compile(
    r"(?:the )?(?:first|second|third|fourth|fifth) row contains\s+"
    r"'([^']*)'\s+under 'Transporter',\s+"
    r"'([^']*)'\s+under 'Tissue Expression',\s+and\s+"
    r"'([^']*)'\s+under 'Biological Role'",
    re.IGNORECASE | re.DOTALL,
)

_ROW_UNDER_COLUMN_PATTERN = re.compile(
    r"(?:the )?(?:first|second|third|fourth|fifth) row[^.]*?"
    r"under 'Transporter'(?: is blank| says '([^']*)')[^.]*?"
    r"under 'Tissue Expression'(?: it)? says '([^']*)'[^.]*?"
    r"under 'Biological Role'(?: it)? says '([^']*)'",
    re.IGNORECASE | re.DOTALL,
)


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


def _parse_column_headers(alt_text: str) -> list[str]:
    match = _COLUMN_HEADER_PATTERN.search(alt_text)
    if not match:
        return []
    return _QUOTED_CELL_PATTERN.findall(match.group(1))


def _parse_table_rows(alt_text: str, headers: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []

    for match in _ROW_CONTAINS_UNDER_PATTERN.finditer(alt_text):
        transporter, tissue, role = match.groups()
        rows.append([transporter.strip(), tissue.strip(), role.strip()])

    if rows:
        return rows

    for match in _ROW_UNDER_COLUMN_PATTERN.finditer(alt_text):
        transporter, tissue, role = match.groups()
        rows.append(
            [
                (transporter or "").strip(),
                tissue.strip(),
                role.strip(),
            ]
        )

    if rows:
        return rows

    for match in _ROW_GLUT_PATTERN.finditer(alt_text):
        transporter, tissue, role = match.groups()
        rows.append([transporter.strip(), tissue.strip(), role.strip().rstrip(".")])

    if rows:
        return rows

    if not headers:
        return rows

    # Generic fallback: keep visual content in one row when we cannot parse cells.
    return []


def _alt_to_summary(alt_text: str) -> str:
    summary = alt_text.strip()
    summary = re.sub(r"^table showing\s+", "", summary, flags=re.IGNORECASE)
    summary = re.sub(r"^a table describing\s+", "", summary, flags=re.IGNORECASE)
    if summary:
        summary = summary[0].upper() + summary[1:]
    return summary


def _make_cell(
    pdf: pikepdf.Pdf,
    *,
    cell_type: str,
    text: str | None = None,
    content: pikepdf.Object | None = None,
    page: pikepdf.Object | None = None,
    col_span: int | None = None,
) -> pikepdf.Dictionary:
    cell = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name(f"/{cell_type}"),
        }
    )
    if page is not None:
        cell["/Pg"] = page
    if cell_type == "TH":
        cell["/Scope"] = pikepdf.Name("/Column")
    if text is not None:
        cell["/K"] = pikepdf.String(text)
    elif content is not None:
        cell["/K"] = content
    if col_span and col_span > 1:
        cell["/Attributes"] = pikepdf.Dictionary({"/ColSpan": col_span})
    return cell


def _make_row(pdf: pikepdf.Pdf, cells: list[pikepdf.Dictionary], page: pikepdf.Object | None) -> pikepdf.Dictionary:
    row = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/TR"),
            "/K": pikepdf.Array(cells),
        }
    )
    if page is not None:
        row["/Pg"] = page
    return row


def _convert_figure_to_table(
    pdf: pikepdf.Pdf,
    figure: pikepdf.Dictionary,
    *,
    figure_index: int,
    alt_text: str,
) -> str:
    page = figure.get("/Pg")
    original_content = figure.get("/K")
    headers = _parse_column_headers(alt_text)
    rows = _parse_table_rows(alt_text, headers)

    figure["/S"] = pikepdf.Name("/Table")
    figure["/Summary"] = pikepdf.String(_alt_to_summary(alt_text))
    if "/Alt" in figure:
        del figure["/Alt"]
    if "/Contents" in figure:
        del figure["/Contents"]

    table_rows: list[pikepdf.Dictionary] = []

    if headers:
        header_cells = [
            _make_cell(pdf, cell_type="TH", text=header, page=page) for header in headers
        ]
        table_rows.append(_make_row(pdf, header_cells, page))

    if rows:
        for row_values in rows:
            while len(row_values) < len(headers):
                row_values.append("")
            cells = [
                _make_cell(pdf, cell_type="TD", text=value, page=page)
                for value in row_values[: len(headers) or len(row_values)]
            ]
            table_rows.append(_make_row(pdf, cells, page))
    elif original_content is not None:
        colspan = len(headers) if headers else None
        body_cell = _make_cell(
            pdf,
            cell_type="TD",
            content=original_content,
            page=page,
            col_span=colspan,
        )
        table_rows.append(_make_row(pdf, [body_cell], page))

    figure["/K"] = pikepdf.Array(table_rows)
    return (
        f"figure{figure_index}: retagged /Figure as /Table "
        f"({len(headers)} headers, {len(rows) or 1} body row(s))"
    )


def repair_figure_to_table(pdf_bytes: bytes) -> tuple[bytes, FigureToTableRepairResult]:
    figures_found = 0
    converted = 0
    actions: list[str] = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            result = FigureToTableRepairResult(0, 0, [])
            return pdf_bytes, result

        figure_index = 0

        def walk(obj: pikepdf.Dictionary) -> None:
            nonlocal figure_index, figures_found, converted
            if obj.get("/S") == "/Figure":
                figure_index += 1
                alt = obj.get("/Alt")
                alt_text = str(alt).strip() if alt is not None else ""
                reasons = classify_figure_alt(
                    alt_text,
                    struct_classes=struct_class_names(obj),
                )
                if "table_tagged_as_figure" not in reasons:
                    return
                figures_found += 1
                action = _convert_figure_to_table(
                    pdf,
                    obj,
                    figure_index=figure_index,
                    alt_text=alt_text,
                )
                converted += 1
                actions.append(action)

            kids = obj.get("/K")
            if isinstance(kids, pikepdf.Array):
                for kid in kids:
                    if isinstance(kid, pikepdf.Dictionary):
                        walk(kid)
                    elif isinstance(kid, pikepdf.Array):
                        for nested in kid:
                            if isinstance(nested, pikepdf.Dictionary):
                                walk(nested)
            elif isinstance(kids, pikepdf.Dictionary):
                walk(kids)

        walk(struct_root)

        output = io.BytesIO()
        pdf.save(output)
        result = FigureToTableRepairResult(
            figures_found=figures_found,
            converted=converted,
            actions=actions,
        )
        return output.getvalue(), result
