"""Apply fallback /Alt on struct-tree figures that lost alt text after chunk merge."""

from __future__ import annotations

import io
import re

import pikepdf

from lib.figure_alt_quality import (
    SuspiciousFigureAlt,
    classify_figure_alt,
    clear_descendant_alternate_text,
    speaks_for_content,
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


def _meaningful_fragments(texts: list[str]) -> list[str]:
    """Keep only removed texts that carry a Latin letter or digit.

    Whitespace and Word's private-use glyph echoes (Ethiopic or Greek code
    points standing in for Cambria Math parentheses) say nothing useful.
    """
    fragments: list[str] = []
    for text in texts:
        cleaned = " ".join(text.split())
        if cleaned and re.search(r"[A-Za-z0-9]", cleaned) and cleaned not in fragments:
            fragments.append(cleaned)
    return fragments


def _has_page_content(obj: pikepdf.Dictionary) -> bool:
    """True when some marked content or annotation sits under this element."""
    kids = obj.get("/K")
    items: list[pikepdf.Object] = []
    if isinstance(kids, pikepdf.Array):
        for kid in kids:
            if isinstance(kid, pikepdf.Array):
                items.extend(kid)
            else:
                items.append(kid)
    elif kids is not None:
        items.append(kids)
    for item in items:
        if not isinstance(item, pikepdf.Dictionary):
            return True
        if "/MCID" in item or item.get("/Type") == "/OBJR":
            return True
        if item.get("/S") is not None and _has_page_content(item):
            return True
    return False


def _remove_child(parent: pikepdf.Dictionary, child: pikepdf.Dictionary) -> None:
    kids = parent.get("/K")
    if isinstance(kids, pikepdf.Dictionary):
        if kids.objgen == child.objgen:
            del parent["/K"]
        return
    if not isinstance(kids, pikepdf.Array):
        return
    for index in range(len(kids) - 1, -1, -1):
        kid = kids[index]
        if isinstance(kid, pikepdf.Dictionary) and kid.objgen == child.objgen:
            del kids[index]


def repair_missing_figure_alt(pdf_bytes: bytes) -> tuple[bytes, list[int]]:
    """Set /Alt (and /Contents when absent) on figures missing alt text.

    Any /Alt or /ActualText under the figure is removed first, so the new alt
    does not nest over it; readable fragments of that text join the fallback.
    A figure whose ancestor already carries /Alt or /ActualText is left alone,
    because that text speaks for the figure and an alt of its own would nest.
    A figure with no page content at all is dropped from the tree instead,
    because Acrobat fails alternate text that is not associated with content.
    Returns updated PDF bytes and 1-based figure indices that were repaired.
    """
    repaired: list[int] = []
    removed_empty = 0

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        figure_index = 0

        def walk(
            obj: pikepdf.Object,
            parent: pikepdf.Dictionary | None,
            spoken_by_ancestor: bool = False,
        ) -> None:
            nonlocal figure_index, removed_empty
            if not isinstance(obj, pikepdf.Dictionary):
                return
            spoken = spoken_by_ancestor or speaks_for_content(obj)
            if obj.get("/S") == "/Figure":
                figure_index += 1
                alt = obj.get("/Alt")
                alt_text = str(alt).strip() if alt is not None else ""
                if not alt_text and not spoken_by_ancestor:
                    if parent is not None and not _has_page_content(obj):
                        _remove_child(parent, obj)
                        removed_empty += 1
                        return
                    removed = clear_descendant_alternate_text(obj)
                    contents = obj.get("/Contents")
                    fallback = str(contents).strip() if contents is not None else ""
                    if not fallback:
                        fragments = _meaningful_fragments(removed)
                        fallback = f"Figure {figure_index}"
                        if fragments:
                            fallback = f"{fallback}: {' '.join(fragments)}"
                    obj["/Alt"] = pikepdf.String(fallback)
                    if contents is None:
                        obj["/Contents"] = pikepdf.String(fallback)
                    repaired.append(figure_index)
            kids = obj.get("/K")
            if isinstance(kids, pikepdf.Array):
                for kid in list(kids):
                    if isinstance(kid, pikepdf.Dictionary):
                        walk(kid, obj, spoken)
                    elif isinstance(kid, pikepdf.Array):
                        for nested in list(kid):
                            if isinstance(nested, pikepdf.Dictionary):
                                walk(nested, obj, spoken)
            elif isinstance(kids, pikepdf.Dictionary):
                walk(kids, obj, spoken)

        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is not None:
            walk(struct_root, None)

        if not repaired and not removed_empty:
            return pdf_bytes, repaired
        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue(), repaired
