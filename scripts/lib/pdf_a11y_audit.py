"""Lightweight PDF/UA structure checks on remediated preview bytes."""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass

import pikepdf

from lib.figure_alt_quality import SuspiciousFigureAlt, classify_figure_alt, struct_class_names


@dataclass
class PdfA11yAudit:
    marked: bool
    figure_count: int
    figures_missing_alt: list[int]
    figures_suspicious_alt: list[SuspiciousFigureAlt]
    table_count: int
    tables_without_summary: int
    tables_without_th: int

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

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        mark_info = pdf.Root.get("/MarkInfo")
        marked = bool(mark_info and mark_info.get("/Marked"))
        struct_root = pdf.Root.get("/StructTreeRoot")

        def walk_figures(obj: pikepdf.Object) -> None:
            nonlocal figure_index
            if not isinstance(obj, pikepdf.Dictionary):
                return
            if obj.get("/S") == "/Figure":
                figure_index += 1
                alt = obj.get("/Alt")
                alt_text = str(alt).strip() if alt is not None else ""
                if not alt_text:
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
                        walk_figures(kid)
                    elif isinstance(kid, pikepdf.Array):
                        for nested in kid:
                            if isinstance(nested, pikepdf.Dictionary):
                                walk_figures(nested)
            elif isinstance(kids, pikepdf.Dictionary):
                walk_figures(kids)

        def walk_tables(obj: pikepdf.Object) -> None:
            nonlocal table_count, tables_without_summary, tables_without_th
            if not isinstance(obj, pikepdf.Dictionary):
                return
            if obj.get("/S") == "/Table":
                table_count += 1
                summary = obj.get("/Summary")
                if summary is None or not str(summary).strip():
                    tables_without_summary += 1
                th_count = 0

                def count_cells(node: pikepdf.Object) -> None:
                    nonlocal th_count
                    if not isinstance(node, pikepdf.Dictionary):
                        return
                    if node.get("/S") == "/TH":
                        th_count += 1
                    kids = node.get("/K")
                    if isinstance(kids, pikepdf.Array):
                        for kid in kids:
                            if isinstance(kid, pikepdf.Dictionary):
                                count_cells(kid)
                    elif isinstance(kids, pikepdf.Dictionary):
                        count_cells(kids)

                count_cells(obj)
                if th_count == 0:
                    tables_without_th += 1
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

    return PdfA11yAudit(
        marked=marked,
        figure_count=figure_index,
        figures_missing_alt=figures_missing_alt,
        figures_suspicious_alt=figures_suspicious_alt,
        table_count=table_count,
        tables_without_summary=tables_without_summary,
        tables_without_th=tables_without_th,
    )
