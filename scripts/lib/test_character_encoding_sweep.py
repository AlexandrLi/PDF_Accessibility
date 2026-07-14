import io
import unittest
from pathlib import Path

import pikepdf

from lib.character_encoding_sweep import (
    _decode_mcid_text,
    _dedupe_font_tounicode,
    _load_tounicode_map,
    _needs_character_encoding_repair,
    _parse_bfchar_pairs,
    _spoken_encoding_text,
    repair_character_encoding,
)

_TOPIC_PDF = Path("tmp/ch9-a11y/14aa5258-before.pdf")


class CharacterEncodingSweepTests(unittest.TestCase):
    def test_needs_repair_for_symbol_glyphs(self) -> None:
        self.assertTrue(_needs_character_encoding_repair("□ "))
        self.assertTrue(_needs_character_encoding_repair("●"))
        self.assertFalse(_needs_character_encoding_repair("Glucose"))

    def test_spoken_encoding_text(self) -> None:
        self.assertEqual(_spoken_encoding_text("□ "), "blank")
        self.assertEqual(_spoken_encoding_text("●"), "bullet")

    def test_decode_mcid_text_handles_tj_arrays(self) -> None:
        body = b"BT /F1 12 Tf [(Hel)-3(lo)8( w)-2(or)-1(ld)] TJ ET"
        decoded = _decode_mcid_text(body, {})
        self.assertIn("Hello", decoded.replace(" ", ""))

    def test_parse_bfchar_pairs_ignores_codespacerange(self) -> None:
        cmap = """
begincodespacerange
<00> <FF>
endcodespacerange
2 beginbfchar
<41> <0041>
<42> <0041>
endbfchar
"""
        pairs = _parse_bfchar_pairs(cmap)
        self.assertEqual(pairs, [("41", "0041"), ("42", "0041")])

    def test_dedupe_preserves_codespacerange(self) -> None:
        cmap = """begincmap
begincodespacerange
<00> <FF>
endcodespacerange
2 beginbfchar
<41> <0041>
<42> <0041>
endbfchar
endcmap"""
        with pikepdf.new() as pdf:
            font = pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name("/Font"),
                    Subtype=pikepdf.Name("/Type1"),
                    BaseFont=pikepdf.Name("/Test"),
                    ToUnicode=pdf.make_stream(cmap.encode("latin1")),
                )
            )
            changed = _dedupe_font_tounicode(pdf, font)
            self.assertTrue(changed)
            new_data = font["/ToUnicode"].read_bytes().decode("latin1")
            self.assertIn("<00> <FF>", new_data)
            mapping = _load_tounicode_map(font)
            self.assertEqual(mapping.get(0x41), "A")
            self.assertNotEqual(mapping.get(0x42), "A")

    def test_repair_common_monosaccharides_pdf(self) -> None:
        if not _TOPIC_PDF.is_file():
            self.skipTest("14aa5258 validation PDF not available")

        pdf_bytes = _TOPIC_PDF.read_bytes()
        repaired, result = repair_character_encoding(pdf_bytes)
        self.assertGreater(result.fonts_updated, 0)
        self.assertGreater(result.struct_updated + result.mcids_updated, 0)
        repaired_twice, second = repair_character_encoding(repaired)
        self.assertEqual(second.struct_updated, 0)
        self.assertEqual(second.mcids_updated, 0)


if __name__ == "__main__":
    unittest.main()
