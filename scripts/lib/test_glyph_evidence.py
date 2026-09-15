"""Tests for naming glyphs from an embedded TrueType program."""

from __future__ import annotations

import struct
import unittest

from lib.glyph_evidence import (
    EMPTY_OUTLINE,
    cambria_math_glyphs,
    glyph_evidence_text,
    truetype_glyph_signature,
)

# Cambria Math's minus sign (glyph 0x0D46) as Word embeds it: one rectangle.
MINUS_GID = 0x0D46
MINUS_RECORD = bytes.fromhex(
    "0001009502070565028c0003000cb202be00b803e8003fed3130133521159504d00207858500"
)


def _truetype_program(glyphs: dict[int, bytes], num_glyphs: int = 0x0E00) -> bytes:
    """A bare TrueType program (head, maxp, loca, glyf) with these outlines."""
    glyf = b""
    loca: list[int] = []
    for gid in range(num_glyphs):
        loca.append(len(glyf))
        glyf += glyphs.get(gid, b"")
    loca.append(len(glyf))
    head = bytearray(54)
    head[50:52] = (1).to_bytes(2, "big")  # long loca offsets
    tables = [
        (b"glyf", glyf),
        (b"head", bytes(head)),
        (b"loca", b"".join(struct.pack(">I", offset) for offset in loca)),
        (b"maxp", b"\x00\x01\x00\x00" + num_glyphs.to_bytes(2, "big")),
    ]
    offset = 12 + 16 * len(tables)
    directory = struct.pack(">IHHHH", 0x00010000, len(tables), 0, 0, 0)
    body = b""
    for tag, data in tables:
        directory += tag + struct.pack(">III", 0, offset + len(body), len(data))
        body += data
    return directory + body


def _simple_glyph(points: list[tuple[int, int]]) -> bytes:
    """A one-contour glyph through these on-curve points, unhinted."""
    record = struct.pack(">hhhhh", 1, 0, 0, 0, 0)
    record += struct.pack(">HH", len(points) - 1, 0)
    record += bytes([0x01] * len(points))
    last_x = last_y = 0
    xs = ys = b""
    for x, y in points:
        xs += struct.pack(">h", x - last_x)
        ys += struct.pack(">h", y - last_y)
        last_x, last_y = x, y
    return record + xs + ys


SQUARE_RECORD = _simple_glyph([(100, 100), (100, 400), (400, 400), (400, 100)])


def _with_instructions(record: bytes, instructions: bytes) -> bytes:
    """The same simple glyph with a different hinting program."""
    contours = int.from_bytes(record[0:2], "big", signed=True)
    at = 10 + 2 * contours
    old_length = int.from_bytes(record[at : at + 2], "big")
    return (
        record[:at]
        + len(instructions).to_bytes(2, "big")
        + instructions
        + record[at + 2 + old_length :]
    )


class GlyphSignatureTests(unittest.TestCase):
    def test_signature_reads_the_outline_and_ignores_hinting(self) -> None:
        # Word's subsets and the desktop Cambria Math differ in instructions
        # at the same glyph id; the points are what identify the glyph.
        hinted = _truetype_program({MINUS_GID: MINUS_RECORD})
        rehinted = _truetype_program(
            {MINUS_GID: _with_instructions(MINUS_RECORD, b"\x00\x01\x02")}
        )
        self.assertEqual(
            truetype_glyph_signature(hinted, MINUS_GID),
            truetype_glyph_signature(rehinted, MINUS_GID),
        )
        square = _truetype_program({MINUS_GID: SQUARE_RECORD})
        self.assertNotEqual(
            truetype_glyph_signature(hinted, MINUS_GID),
            truetype_glyph_signature(square, MINUS_GID),
        )

    def test_empty_outline_and_missing_glyph(self) -> None:
        program = _truetype_program({MINUS_GID: MINUS_RECORD})
        self.assertEqual(truetype_glyph_signature(program, 3), EMPTY_OUTLINE)
        self.assertIsNone(truetype_glyph_signature(program, 0xE00))
        self.assertIsNone(truetype_glyph_signature(b"not a font", 3))


class GlyphEvidenceTextTests(unittest.TestCase):
    def test_outline_matching_cambria_math_is_named(self) -> None:
        program = _truetype_program({MINUS_GID: MINUS_RECORD})
        self.assertEqual(glyph_evidence_text(program, MINUS_GID), "−")

    def test_glyph_without_outline_reads_as_a_space(self) -> None:
        self.assertEqual(glyph_evidence_text(_truetype_program({}), 3), " ")

    def test_other_outline_at_a_named_id_stays_unnamed(self) -> None:
        # The same id holds a different glyph in another font: no name.
        program = _truetype_program({MINUS_GID: SQUARE_RECORD})
        self.assertIsNotNone(truetype_glyph_signature(program, MINUS_GID))
        self.assertIsNone(glyph_evidence_text(program, MINUS_GID))

    def test_reference_table_names_only_speakable_text(self) -> None:
        table = cambria_math_glyphs()
        self.assertGreater(len(table), 5000)
        for text, signature in table.values():
            self.assertTrue(text == " " or (len(text) == 1 and ord(text) >= 0x20))
            self.assertFalse(0xE000 <= ord(text) <= 0xF8FF or 0x2500 <= ord(text) <= 0x257F)
            self.assertTrue(signature or text == " ")


if __name__ == "__main__":
    unittest.main()
