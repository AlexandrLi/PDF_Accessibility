#!/usr/bin/env python3
"""Inspect PDF accessibility markers (structure tree, MarkInfo)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pikepdf


def count_struct_nodes(obj, depth: int = 0) -> int:
    if depth > 50:
        return 0
    total = 1
    kids = obj.get("/K")
    if kids is None:
        return total
    if isinstance(kids, pikepdf.Array):
        for kid in kids:
            if isinstance(kid, pikepdf.Dictionary) and "/S" in kid:
                total += count_struct_nodes(kid, depth + 1)
            elif isinstance(kid, pikepdf.Array):
                for nested in kid:
                    if isinstance(nested, pikepdf.Dictionary) and "/S" in nested:
                        total += count_struct_nodes(nested, depth + 1)
    return total


def inspect(path: Path) -> dict:
    with pikepdf.open(path) as pdf:
        root = pdf.Root
        mark_info = root.get("/MarkInfo")
        marked = bool(mark_info and mark_info.get("/Marked"))
        struct_tree = root.get("/StructTreeRoot")
        struct_present = struct_tree is not None
        struct_nodes = count_struct_nodes(struct_tree) if struct_tree else 0
        lang = root.get("/Lang")
        metadata = pdf.docinfo.get("/Title") if pdf.docinfo else None
        return {
            "file": path.name,
            "pages": len(pdf.pages),
            "pdfVersion": str(pdf.pdf_version),
            "marked": marked,
            "structTreePresent": struct_present,
            "approxStructNodes": struct_nodes,
            "lang": str(lang) if lang else None,
            "title": str(metadata) if metadata else None,
        }


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        print("Usage: inspect_pdf.py file1.pdf [file2.pdf ...]", file=sys.stderr)
        return 1
    results = [inspect(path) for path in paths]
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
