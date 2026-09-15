#!/usr/bin/env python3
"""Build scripts/lib/data/cambria_math_glyphs.json from a full Cambria Math.

The marked-content ActualText sweep names a glyph in a Word-embedded Cambria
Math subset when the outline at that glyph id matches this table. Names come
from the font's own tables, in this order:

- `cmap`: the character the glyph encodes (Private Use and control
  characters are left out; any whitespace becomes a plain space);
- GSUB `ssty` substitutions: a script-size variant reads as its base;
- MATH variants: a stretched delimiter or accent reads as its base.

Assembly parts (the pieces of a very tall brace) are left unnamed, since no
single character describes a fragment. Needs fontTools, which the migrate
requirements do not list; the sweep itself reads only the JSON.

    scripts/with-a11y-python.sh scripts/build_cambria_math_glyph_table.py
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from lib.glyph_evidence import (  # noqa: E402
    CAMBRIA_MATH_TABLE,
    EMPTY_OUTLINE,
    truetype_glyph_signature,
)

DEFAULT_FONT = Path(
    "/Applications/Microsoft Word.app/Contents/Resources/DFonts/Cambria.ttc"
)


def _spoken(codepoints: list[int]) -> str | None:
    if any(chr(code).isspace() for code in codepoints):
        return " "
    named = [
        code
        for code in sorted(codepoints)
        if code >= 0x20 and not 0xE000 <= code <= 0xF8FF and not 0x2500 <= code <= 0x257F
    ]
    return chr(named[0]) if named else None


def build_table(font_path: Path, font_number: int | None) -> dict:
    from fontTools.ttLib import TTCollection, TTFont

    if font_number is None and font_path.suffix.lower() == ".ttc":
        members = TTCollection(str(font_path)).fonts
        font = next(
            member for member in members if member["name"].getDebugName(4) == "Cambria Math"
        )
    else:
        font = TTFont(str(font_path), fontNumber=font_number or 0)
    order = font.getGlyphOrder()
    index = {name: gid for gid, name in enumerate(order)}

    by_glyph: dict[str, list[int]] = {}
    for code, name in font.getBestCmap().items():
        by_glyph.setdefault(name, []).append(code)
    text: dict[str, str] = {}
    for name, codes in by_glyph.items():
        spoken = _spoken(codes)
        if spoken is not None:
            text[name] = spoken
    counts = {"cmap": len(text), "ssty": 0, "math": 0}

    gsub = font["GSUB"].table
    ssty_lookups: set[int] = set()
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag == "ssty":
            ssty_lookups.update(record.Feature.LookupListIndex)
    for lookup_index in sorted(ssty_lookups):
        lookup = gsub.LookupList.Lookup[lookup_index]
        for subtable in lookup.SubTable:
            if lookup.LookupType == 7:
                subtable = subtable.ExtSubTable
            pairs: list[tuple[str, str]] = []
            for base, variants in getattr(subtable, "alternates", {}).items():
                pairs.extend((base, variant) for variant in variants)
            for base, variant in getattr(subtable, "mapping", {}).items():
                pairs.append((base, variant))
            for base, variant in pairs:
                if variant not in text and base in text:
                    text[variant] = text[base]
                    counts["ssty"] += 1

    variants = font["MATH"].table.MathVariants
    for coverage, constructions in (
        (variants.VertGlyphCoverage, variants.VertGlyphConstruction),
        (variants.HorizGlyphCoverage, variants.HorizGlyphConstruction),
    ):
        for base, construction in zip(coverage.glyphs, constructions):
            for record in construction.MathGlyphVariantRecord:
                if record.VariantGlyph not in text and base in text:
                    text[record.VariantGlyph] = text[base]
                    counts["math"] += 1

    buffer = io.BytesIO()
    font.save(buffer)
    program = buffer.getvalue()
    glyphs: dict[str, list[str]] = {}
    for name, spoken in text.items():
        gid = index[name]
        signature = truetype_glyph_signature(program, gid)
        if signature is None or (signature == EMPTY_OUTLINE and spoken != " "):
            continue
        glyphs[str(gid)] = [spoken, signature]
    version = font["name"].getDebugName(5)
    return {
        "source": f"{font['name'].getDebugName(4)} {version}",
        "counts": counts,
        "glyphs": dict(sorted(glyphs.items(), key=lambda item: int(item[0]))),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT)
    parser.add_argument("--font-number", type=int, default=None)
    parser.add_argument("--output", type=Path, default=CAMBRIA_MATH_TABLE)
    args = parser.parse_args()
    table = build_table(args.font, args.font_number)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    glyph_lines = ",\n".join(
        f"{json.dumps(gid)}:{json.dumps(entry, ensure_ascii=False)}"
        for gid, entry in table["glyphs"].items()
    )
    args.output.write_text(
        "{\n"
        f'"source":{json.dumps(table["source"])},\n'
        f'"counts":{json.dumps(table["counts"])},\n'
        f'"glyphs":{{\n{glyph_lines}\n}}\n}}\n',
        encoding="utf-8",
    )
    print(f"{args.output}: {len(table['glyphs'])} glyphs from {table['source']} {table['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
