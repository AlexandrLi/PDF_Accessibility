#!/usr/bin/env python3
"""Apply Bedrock-generated /Alt text to tagged PDF figures using pikepdf.

Walks the structure tree and matches Adobe object IDs (objgen) to alt text.
pdf-lib was dropping /S=/Figure on some struct elements during save.
"""

from __future__ import annotations

import json
import sys

import pikepdf


def apply_figure_alts(compliant_path: str, output_path: str, alt_by_objid: dict[str, str]) -> int:
    applied = 0

    with pikepdf.open(compliant_path) as pdf:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            raise RuntimeError("PDF has no /StructTreeRoot")

        def walk(obj: pikepdf.Dictionary) -> None:
            nonlocal applied
            if obj.get("/S") == "/Figure":
                objid = str(obj.objgen[0])
                alt_text = alt_by_objid.get(objid)
                if alt_text is None:
                    return
                if alt_text == "artifact":
                    obj["/S"] = "/Artifact"
                    if "/Alt" in obj:
                        del obj["/Alt"]
                    if "/Contents" in obj:
                        del obj["/Contents"]
                    applied += 1
                    return
                obj["/Alt"] = alt_text
                obj["/Contents"] = alt_text
                applied += 1

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
        pdf.save(output_path)

    return applied


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "Usage: apply_figure_alt.py <compliant.pdf> <output.pdf> <alt.json>",
            file=sys.stderr,
        )
        return 1

    compliant_path, output_path, alt_json_path = sys.argv[1:4]
    alt_by_objid = json.load(open(alt_json_path, encoding="utf-8"))
    applied = apply_figure_alts(compliant_path, output_path, alt_by_objid)
    print(f"Applied alt text to {applied} figures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
