"""Name a glyph from the embedded font program when /ToUnicode declines to.

Word writes Private Use destinations (U+F000 and up) for the Cambria Math
glyphs its equation layout reaches through OpenType substitutions: script-
size digits and letters (`ssty`), stretched delimiters and accents (MATH
variants). The subsets it embeds keep the full font's glyph ids but drop the
`cmap` and `post` tables, so the program itself carries no names. The
outlines are intact, though, and a glyph whose outline is point-for-point
the one at the same id in Cambria Math is that glyph. The reference table
`data/cambria_math_glyphs.json` (built by
`scripts/build_cambria_math_glyph_table.py`) pairs each named id with a
digest of its outline, and this module checks a subset glyph against it.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

CAMBRIA_MATH_TABLE = Path(__file__).resolve().parent / "data" / "cambria_math_glyphs.json"

# Empty outlines all digest alike, so they get a marker instead.
EMPTY_OUTLINE = ""


def _truetype_tables(font_buffer: bytes) -> dict[bytes, tuple[int, int]] | None:
    if len(font_buffer) < 12:
        return None
    num_tables = int.from_bytes(font_buffer[4:6], "big")
    tables: dict[bytes, tuple[int, int]] = {}
    for index in range(num_tables):
        record = font_buffer[12 + index * 16 : 28 + index * 16]
        if len(record) < 16:
            return None
        tables[record[0:4]] = (
            int.from_bytes(record[8:12], "big"),
            int.from_bytes(record[12:16], "big"),
        )
    return tables


def _glyph_bytes(font_buffer: bytes, gid: int) -> bytes | None:
    """The raw `glyf` record for one glyph, b"" when it has no outline."""
    tables = _truetype_tables(font_buffer)
    if tables is None or not {b"head", b"loca", b"maxp", b"glyf"} <= tables.keys():
        return None
    head = tables[b"head"][0]
    long_offsets = int.from_bytes(font_buffer[head + 50 : head + 52], "big") == 1
    maxp = tables[b"maxp"][0]
    if gid < 0 or gid >= int.from_bytes(font_buffer[maxp + 4 : maxp + 6], "big"):
        return None
    loca, loca_length = tables[b"loca"]
    size = 4 if long_offsets else 2
    if (gid + 2) * size > loca_length:
        return None
    start = int.from_bytes(font_buffer[loca + gid * size : loca + (gid + 1) * size], "big")
    end = int.from_bytes(
        font_buffer[loca + (gid + 1) * size : loca + (gid + 2) * size], "big"
    )
    if not long_offsets:
        start, end = start * 2, end * 2
    if end < start:
        return None
    glyf, glyf_length = tables[b"glyf"]
    if end > glyf_length:
        return None
    return font_buffer[glyf + start : glyf + end]


def _int16(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 2], "big", signed=True)


def _uint16(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 2], "big")


def _simple_outline(record: bytes, contours: int) -> str:
    end_points = [_uint16(record, 10 + 2 * index) for index in range(contours)]
    points = end_points[-1] + 1 if end_points else 0
    at = 10 + 2 * contours
    at += 2 + _uint16(record, at)  # instructions
    flags: list[int] = []
    while len(flags) < points:
        flag = record[at]
        at += 1
        flags.append(flag)
        if flag & 0x08:
            flags.extend([flag] * record[at])
            at += 1
    flags = flags[:points]

    def coordinates(short_bit: int, same_bit: int) -> list[int]:
        nonlocal at
        values: list[int] = []
        value = 0
        for flag in flags:
            if flag & short_bit:
                delta = record[at]
                at += 1
                value += delta if flag & same_bit else -delta
            elif not flag & same_bit:
                value += _int16(record, at)
                at += 2
            values.append(value)
        return values

    xs = coordinates(0x02, 0x10)
    ys = coordinates(0x04, 0x20)
    return "S%d:%s:%s" % (
        contours,
        ",".join(map(str, end_points)),
        ";".join(f"{x},{y},{flag & 1}" for x, y, flag in zip(xs, ys, flags)),
    )


def _composite_outline(record: bytes) -> str:
    at = 10
    parts: list[str] = []
    while True:
        flags = _uint16(record, at)
        component = _uint16(record, at + 2)
        at += 4
        if flags & 0x0001:  # ARG_1_AND_2_ARE_WORDS
            first, second = _int16(record, at), _int16(record, at + 2)
            at += 4
        else:
            first, second = record[at], record[at + 1]
            if flags & 0x0002:  # ARGS_ARE_XY_VALUES: signed offsets
                first = first - 256 if first > 127 else first
                second = second - 256 if second > 127 else second
            at += 2
        scale: list[int] = []
        if flags & 0x0008:  # WE_HAVE_A_SCALE
            scale = [_int16(record, at)]
            at += 2
        elif flags & 0x0040:  # WE_HAVE_AN_X_AND_Y_SCALE
            scale = [_int16(record, at), _int16(record, at + 2)]
            at += 4
        elif flags & 0x0080:  # WE_HAVE_A_TWO_BY_TWO
            scale = [_int16(record, at + 2 * index) for index in range(4)]
            at += 8
        parts.append(
            f"{component},{flags & 0x0002},{first},{second},{','.join(map(str, scale))}"
        )
        if not flags & 0x0020:  # MORE_COMPONENTS
            break
    return "C:" + ";".join(parts)


def truetype_glyph_signature(font_buffer: bytes, gid: int) -> str | None:
    """Digest a TrueType glyph's decoded outline, independent of hinting.

    Returns EMPTY_OUTLINE for a glyph with no contours and None when the
    program cannot be read. Two glyphs with the same signature have the same
    contours, points and component placement; the instructions, bounding box
    and flag packing that differ between font versions are left out.
    """
    try:
        record = _glyph_bytes(font_buffer, gid)
        if record is None:
            return None
        if not record:
            return EMPTY_OUTLINE
        contours = _int16(record, 0)
        outline = (
            _composite_outline(record) if contours < 0 else _simple_outline(record, contours)
        )
    except (IndexError, ValueError):
        return None
    return hashlib.sha1(outline.encode("ascii")).hexdigest()[:16]


@lru_cache(maxsize=1)
def cambria_math_glyphs() -> dict[int, tuple[str, str]]:
    """gid → (text, outline signature) for every named Cambria Math glyph."""
    if not CAMBRIA_MATH_TABLE.exists():
        return {}
    table = json.loads(CAMBRIA_MATH_TABLE.read_text(encoding="utf-8"))
    return {
        int(gid): (entry[0], entry[1]) for gid, entry in table.get("glyphs", {}).items()
    }


def glyph_evidence_text(font_buffer: bytes, gid: int) -> str | None:
    """Text for a glyph the font program itself vouches for, else None.

    A glyph with no outline paints nothing and reads as a space. A glyph
    whose outline matches the Cambria Math reference at the same id reads
    as that glyph's character. Anything else stays unnamed.
    """
    signature = truetype_glyph_signature(font_buffer, gid)
    if signature is None:
        return None
    if signature == EMPTY_OUTLINE:
        return " "
    reference = cambria_math_glyphs().get(gid)
    if reference is not None and reference[1] == signature:
        return reference[0]
    return None
