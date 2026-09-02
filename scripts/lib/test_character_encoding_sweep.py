import io
import unittest
from pathlib import Path

import pikepdf

from lib.character_encoding_sweep import (
    _body_has_blank_square_glyph,
    _decode_mcid_text,
    _replace_unreliable_font_tounicode,
    _load_tounicode_map,
    _needs_character_encoding_repair,
    _parse_bfchar_pairs,
    _parse_bfrange_pairs,
    _parse_tounicode_entries,
    _spoken_encoding_text,
    count_ambiguous_tounicode_fonts,
    repair_character_encoding,
)

_TOPIC_PDF = Path("tmp/ch9-a11y/14aa5258-before.pdf")
_ENCODING_FIX_DIR = Path("tmp/encoding-fix-review")
_ENCODING_FIX_TOPIC_IDS = ("182549fe", "3ae3bae9", "6f84ffb1")


class BlankSquareGlyphDetectionTests(unittest.TestCase):
    def test_lone_glyph_is_blank(self) -> None:
        body = b" BT\r\n/F3 14.04 Tf\r\n1 0 0 1 72.024 653.02 Tm\r\n0 g\r\n[<0191>] TJ\r\nET\r\n"
        self.assertTrue(_body_has_blank_square_glyph(body))

    def test_glyph_with_trailing_underscores_is_blank(self) -> None:
        self.assertTrue(_body_has_blank_square_glyph(b" <0191>Tj (____) Tj"))

    def test_glyph_used_as_bullet_marker_before_real_sentence_is_not_blank(
        self,
    ) -> None:
        # Regression: a bullet marker sharing this glyph, immediately
        # followed by a real sentence in the SAME marked-content block
        # (because the enclosing BT was opened by a sibling MCID), must not
        # have its whole sentence overwritten with "blank".
        body = (
            b" \n/C2_1 14.04 Tf\n72.024 653.02 Td\n<0191>Tj\n/TT1 12 Tf\n"
            b"11.76 0 Td\n[(Rec)8 (a)-3 (ll)4 ( t)-3 (h)-3 (e)-3 "
            b"( c)8 (a)-3 (lcula)-3 (ti)10 (o)-3 (n)-3)]TJ\n"
        )
        self.assertFalse(_body_has_blank_square_glyph(body))

    def test_no_glyph_is_not_blank(self) -> None:
        self.assertFalse(_body_has_blank_square_glyph(b"(hello) Tj"))


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

    def test_parse_bfrange_scalar_and_array_destinations(self) -> None:
        scalar = """1 beginbfrange
<0001> <0003> <0041>
endbfrange"""
        array = """1 beginbfrange
<0004> <0006> [<0044> <0045> <0046>]
endbfrange"""
        self.assertEqual(
            _parse_bfrange_pairs(scalar),
            [("0001", "0041"), ("0002", "0042"), ("0003", "0043")],
        )
        self.assertEqual(
            _parse_bfrange_pairs(array),
            [("0004", "0044"), ("0005", "0045"), ("0006", "0046")],
        )

    def test_invalid_tounicode_map_is_diagnostic_and_not_invented(self) -> None:
        pairs, diagnostics = _parse_tounicode_entries(
            """1 beginbfchar
<0001> <D800>
endbfchar"""
        )
        self.assertEqual(pairs, [])
        self.assertTrue(diagnostics)
        self.assertEqual(_decode_mcid_text(b"BT /F1 12 Tf <0002>Tj ET", {"/F1": {}}), "�")

    def test_type0_map_loads_without_guessing_unknown_cids(self) -> None:
        cmap = """1 beginbfrange
<0001> <0002> <0041>
endbfrange"""
        with pikepdf.new() as pdf:
            descendant = pikepdf.Dictionary(
                Type=pikepdf.Name("/Font"),
                Subtype=pikepdf.Name("/CIDFontType2"),
            )
            font = pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name("/Font"),
                    Subtype=pikepdf.Name("/Type0"),
                    Encoding=pikepdf.Name("/Identity-H"),
                    DescendantFonts=pikepdf.Array([descendant]),
                    ToUnicode=pdf.make_stream(cmap.encode("latin1")),
                )
            )
            self.assertEqual(_load_tounicode_map(font), {1: "A", 2: "B"})
            self.assertEqual(
                _decode_mcid_text(
                    b"BT /F1 12 Tf <00010003>Tj ET",
                    {"/F1": _load_tounicode_map(font)},
                ),
                "A�",
            )

    def test_missing_tounicode_is_reported_without_changing_pdf(self) -> None:
        with pikepdf.new() as pdf:
            page = pdf.add_blank_page()
            font = pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name("/Font"),
                    Subtype=pikepdf.Name("/Type1"),
                    BaseFont=pikepdf.Name("/Helvetica"),
                )
            )
            page["/Resources"] = pikepdf.Dictionary(
                Font=pikepdf.Dictionary(F1=font)
            )
            original = io.BytesIO()
            pdf.save(original)

        repaired, result = repair_character_encoding(original.getvalue())
        self.assertEqual(repaired, original.getvalue())
        self.assertEqual(result.fonts_inspected, 1)
        self.assertEqual(len(result.missing_tounicode_fonts), 1)
        self.assertFalse(result.invalid_tounicode_fonts)

    def test_used_codes_track_q_state_and_show_operators(self) -> None:
        from lib.character_encoding_sweep import _used_codes_for_font

        data = (
            b"/TT1 10 Tf (AB) Tj "
            b"q /C2_7 10 Tf <0003> Tj Q "
            b"[(CD) 5 (E)] TJ "
            b"/BDC-noise << /ActualText (ZZ) >> BDC EMC"
        )
        self.assertEqual(
            _used_codes_for_font(data, {"/C2_7"}, two_byte=True), {0x0003}
        )
        self.assertEqual(
            _used_codes_for_font(data, {"/TT1"}, two_byte=False),
            {ord("A"), ord("B"), ord("C"), ord("D"), ord("E")},
        )

    def test_build_tounicode_cmap_round_trips(self) -> None:
        from lib.character_encoding_sweep import (
            _build_tounicode_cmap,
            _parse_tounicode_entries,
        )

        cmap = _build_tounicode_cmap({3: " ", 0x636: "✓"}, two_byte=True)
        pairs, diagnostics = _parse_tounicode_entries(
            cmap.decode("latin1", errors="replace")
        )
        self.assertEqual(diagnostics, [])
        mapping = {int(src, 16): dst for src, dst in pairs}
        self.assertEqual(mapping[3], "0020")
        self.assertEqual(mapping[0x636], "2713")

    def test_missing_tounicode_borrows_sibling_map(self) -> None:
        beginning_algebra = Path(
            "pdfs/accessibility-issue-map/beginning-algebra/20260901/originals/9d9dcb9c.pdf"
        )
        if not beginning_algebra.is_file():
            self.skipTest("beginning-algebra 9d9dcb9c original not available")
        repaired, result = repair_character_encoding(beginning_algebra.read_bytes())
        self.assertEqual(result.missing_tounicode_fonts, [])
        borrow_actions = [
            action for action in result.actions if "added missing /ToUnicode" in action
        ]
        self.assertEqual(len(borrow_actions), 3)
        repaired_again, second = repair_character_encoding(repaired)
        self.assertEqual(
            [a for a in second.actions if "added missing /ToUnicode" in a], []
        )

    def test_duplicate_destinations_are_preserved(self) -> None:
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
            changed = _replace_unreliable_font_tounicode(pdf, font)
            self.assertFalse(changed)
            mapping = _load_tounicode_map(font)
            self.assertEqual(mapping.get(0x41), "A")
            self.assertEqual(mapping.get(0x42), "A")

    def test_unreliable_replacement_preserves_codespacerange(self) -> None:
        cmap = """begincmap
begincodespacerange
<00> <FF>
endcodespacerange
2 beginbfchar
<41> <0041>
<42> <FFFD>
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
            changed = _replace_unreliable_font_tounicode(pdf, font)
            self.assertTrue(changed)
            new_data = font["/ToUnicode"].read_bytes().decode("latin1")
            self.assertIn("<00> <FF>", new_data)
            mapping = _load_tounicode_map(font)
            self.assertEqual(mapping.get(0x41), "A")
            replaced = mapping.get(0x42)
            self.assertIsNotNone(replaced)
            assert replaced is not None
            self.assertEqual(len(replaced), 1)
            self.assertGreaterEqual(ord(replaced), 0x2500)
            self.assertLessEqual(ord(replaced), 0x257F)

    def test_dedupe_replaces_unreliable_unicode_destinations(self) -> None:
        cmap = """begincmap
begincodespacerange
<00> <FF>
endcodespacerange
3 beginbfchar
<41> <0008>
<42> <E003>
<43> <FFFD>
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

            self.assertTrue(_replace_unreliable_font_tounicode(pdf, font))
            mapping = _load_tounicode_map(font)
            self.assertEqual(len(set(mapping.values())), 3)
            for value in mapping.values():
                self.assertGreaterEqual(ord(value), 0x2500)
                self.assertLessEqual(ord(value), 0x257F)

    def test_replacement_does_not_corrupt_adjacent_newline_pairs(self) -> None:
        """Regression: regex must not treat dst/src on adjacent lines as one pair."""
        cmap = """begincmap
begincodespacerange
<00> <FF>
endcodespacerange
2 beginbfchar
<0054> <0055>
<0055> <FFFD>
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
            changed = _replace_unreliable_font_tounicode(pdf, font)
            self.assertTrue(changed)

            new_data = font["/ToUnicode"].read_bytes().decode("latin1")
            self.assertNotIn("<0055><F", new_data)
            self.assertNotRegex(new_data, r"<0054>\s*<0055><F")

            mapping = _load_tounicode_map(font)
            self.assertEqual(mapping.get(0x0054), "U")
            replaced = mapping.get(0x0055)
            self.assertIsNotNone(replaced)
            assert replaced is not None
            self.assertNotEqual(replaced, "�")
            self.assertEqual(len(replaced), 1)
            self.assertGreaterEqual(ord(replaced), 0x2500)
            self.assertLessEqual(ord(replaced), 0x257F)

    def test_repair_character_encoding_idempotent_for_biochemistry_topics(self) -> None:
        for topic_id in _ENCODING_FIX_TOPIC_IDS:
            pdf_path = _ENCODING_FIX_DIR / f"{topic_id}-before.pdf"
            if not pdf_path.is_file():
                continue
            pdf_bytes = pdf_path.read_bytes()
            repaired_once, first = repair_character_encoding(pdf_bytes)
            repaired_twice, second = repair_character_encoding(repaired_once)
            self.assertEqual(
                second.fonts_updated,
                0,
                f"{topic_id} encoding repair should be idempotent",
            )
            self.assertEqual(
                count_ambiguous_tounicode_fonts(repaired_twice),
                0,
                f"{topic_id} should stay unambiguous after second pass",
            )

    def test_repair_ambiguous_tounicode_identity_h_fonts(self) -> None:
        for topic_id in _ENCODING_FIX_TOPIC_IDS:
            pdf_path = _ENCODING_FIX_DIR / f"{topic_id}-before.pdf"
            if not pdf_path.is_file():
                continue
            pdf_bytes = pdf_path.read_bytes()
            before_count = count_ambiguous_tounicode_fonts(pdf_bytes)
            self.assertGreater(
                before_count,
                0,
                f"{topic_id} should have ambiguous /ToUnicode before repair",
            )
            repaired, result = repair_character_encoding(pdf_bytes)
            self.assertGreater(result.fonts_updated, 0)
            after_count = count_ambiguous_tounicode_fonts(repaired)
            self.assertEqual(
                after_count,
                0,
                f"{topic_id} should have no ambiguous /ToUnicode after repair",
            )
            with pikepdf.open(io.BytesIO(repaired)) as opened:
                seen: set[tuple[int, int]] = set()
                for page in opened.pages:
                    fonts = page.get("/Resources", {}).get("/Font", {})
                    if not fonts:
                        continue
                    for font in fonts.values():
                        key = font.objgen
                        if key in seen:
                            continue
                        seen.add(key)
                        for dst_char in _load_tounicode_map(font).values():
                            self.assertFalse(
                                dst_char.startswith("U+"),
                                f"{topic_id} ToUnicode must not contain literal U+ strings",
                            )

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
