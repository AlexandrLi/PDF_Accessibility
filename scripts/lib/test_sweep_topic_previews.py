"""Integration tests for preview sweep pipeline (encoding + marked content)."""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

import pikepdf

from lib.character_encoding_sweep import count_ambiguous_tounicode_fonts
from lib.test_marked_content_actualtext_sweep import (
    _ENCODING_FIX_DIR,
    _li_alt_with_lbl_actualtext_violations,
)

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from sweep_topic_previews import apply_sweeps, audit_preview_bytes  # noqa: E402

_BIOCHEM_ENCODING_TOPIC_IDS = (
    ("182549fe", "Comprehensive Final Lipid Map"),
    ("3ae3bae9", "Summary of Membrane Transport"),
    ("6f84ffb1", "Introduction to Biosignaling"),
)


class SweepTopicPreviewsIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(
        all(
            (_ENCODING_FIX_DIR / f"{topic_id}-before.pdf").is_file()
            for topic_id, _ in _BIOCHEM_ENCODING_TOPIC_IDS
        ),
        "biochemistry encoding-fix validation PDFs not available",
    )
    def test_full_sweep_clears_ambiguous_tounicode(self) -> None:
        for topic_id, title in _BIOCHEM_ENCODING_TOPIC_IDS:
            pdf_bytes = (_ENCODING_FIX_DIR / f"{topic_id}-before.pdf").read_bytes()
            swept, results = apply_sweeps(pdf_bytes)
            encoding = results.get("characterEncodingRepair") or {}
            self.assertGreater(
                encoding.get("fonts_updated", 0),
                0,
                f"{topic_id} should update at least one font",
            )
            self.assertEqual(
                count_ambiguous_tounicode_fonts(swept),
                0,
                f"{topic_id} should have no ambiguous /ToUnicode after full sweep",
            )
            audit = audit_preview_bytes(topic_id, title, swept)
            self.assertEqual(
                audit.ambiguous_tounicode_mappings,
                0,
                f"{topic_id} audit should not report ambiguous /ToUnicode",
            )

    @unittest.skipUnless(
        (_ENCODING_FIX_DIR / "6f84ffb1-before.pdf").is_file(),
        "6f84ffb1 validation PDF not available",
    )
    def test_full_sweep_biosignaling_avoids_nested_li_lbl_alt(self) -> None:
        pdf_bytes = (_ENCODING_FIX_DIR / "6f84ffb1-before.pdf").read_bytes()
        swept, _ = apply_sweeps(pdf_bytes)
        violations = _li_alt_with_lbl_actualtext_violations(swept)
        self.assertEqual(
            violations,
            [],
            f"nested alt regression after full sweep: {violations}",
        )

    @unittest.skipUnless(
        (_ENCODING_FIX_DIR / "3ae3bae9-before.pdf").is_file(),
        "3ae3bae9 validation PDF not available",
    )
    def test_full_sweep_membrane_transport_stays_idempotent_on_encoding(self) -> None:
        pdf_bytes = (_ENCODING_FIX_DIR / "3ae3bae9-before.pdf").read_bytes()
        swept_once, _ = apply_sweeps(pdf_bytes)
        swept_twice, second = apply_sweeps(swept_once)
        encoding = second.get("characterEncodingRepair") or {}
        self.assertEqual(encoding.get("fonts_updated", 0), 0)
        self.assertEqual(count_ambiguous_tounicode_fonts(swept_twice), 0)


if __name__ == "__main__":
    unittest.main()
