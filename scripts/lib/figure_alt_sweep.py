"""Apply fallback /Alt on struct-tree figures that lost alt text after chunk merge."""

from __future__ import annotations

import io

import pikepdf

from lib.figure_alt_quality import (
    SuspiciousFigureAlt,
    classify_figure_alt,
    struct_class_names,
)


def find_suspicious_figure_alts(pdf_bytes: bytes) -> list[SuspiciousFigureAlt]:
    """Return 1-based figure indices whose /Alt text looks incomplete or mis-tagged."""
    suspicious: list[SuspiciousFigureAlt] = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        figure_index = 0

        def walk(obj: pikepdf.Object) -> None:
            nonlocal figure_index
            if not isinstance(obj, pikepdf.Dictionary):
                return
            if obj.get("/S") == "/Figure":
                figure_index += 1
                alt = obj.get("/Alt")
                alt_text = str(alt).strip() if alt is not None else ""
                reasons = classify_figure_alt(
                    alt_text,
                    struct_classes=struct_class_names(obj),
                )
                if reasons:
                    suspicious.append(
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
                        walk(kid)
                    elif isinstance(kid, pikepdf.Array):
                        for nested in kid:
                            if isinstance(nested, pikepdf.Dictionary):
                                walk(nested)
            elif isinstance(kids, pikepdf.Dictionary):
                walk(kids)

        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is not None:
            walk(struct_root)

    return suspicious


def strip_suspicious_figure_alt(pdf_bytes: bytes) -> tuple[bytes, list[SuspiciousFigureAlt]]:
    """Remove /Alt and /Contents on figures with suspicious alt text.

    Cleared figures surface as missing alt in audit so migration can block the write
    and force a full Adobe/Bedrock re-run instead of keeping bad alt text.
    """
    stripped: list[SuspiciousFigureAlt] = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        figure_index = 0

        def walk(obj: pikepdf.Dictionary) -> None:
            nonlocal figure_index
            if obj.get("/S") == "/Figure":
                figure_index += 1
                alt = obj.get("/Alt")
                alt_text = str(alt).strip() if alt is not None else ""
                reasons = classify_figure_alt(
                    alt_text,
                    struct_classes=struct_class_names(obj),
                )
                if reasons:
                    stripped.append(
                        SuspiciousFigureAlt(
                            figure_index=figure_index,
                            alt_text=alt_text,
                            reasons=tuple(reasons),
                        )
                    )
                    if "/Alt" in obj:
                        del obj["/Alt"]
                    if "/Contents" in obj:
                        del obj["/Contents"]
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

        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is not None:
            walk(struct_root)

        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue(), stripped


def repair_missing_figure_alt(pdf_bytes: bytes) -> tuple[bytes, list[int]]:
    """Set /Alt (and /Contents when absent) on figures missing alt text.

    Returns updated PDF bytes and 1-based figure indices that were repaired.
    """
    repaired: list[int] = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        figure_index = 0

        def walk(obj: pikepdf.Object) -> None:
            nonlocal figure_index
            if not isinstance(obj, pikepdf.Dictionary):
                return
            if obj.get("/S") == "/Figure":
                figure_index += 1
                alt = obj.get("/Alt")
                alt_text = str(alt).strip() if alt is not None else ""
                if not alt_text:
                    contents = obj.get("/Contents")
                    fallback = str(contents).strip() if contents is not None else ""
                    if not fallback:
                        fallback = f"Figure {figure_index}"
                    obj["/Alt"] = pikepdf.String(fallback)
                    if contents is None:
                        obj["/Contents"] = pikepdf.String(fallback)
                    repaired.append(figure_index)
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

        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is not None:
            walk(struct_root)

        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue(), repaired
