"""Tests for the unattended per-course policy."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

import pikepdf

from lib.auto_course_pipeline import (
    adobe_gate,
    chapter_high_confidence,
    append_tracker_note,
    apply_retag_to_topic,
    build_publish_manifest,
    count_fonts_without_tounicode,
    decide_topic,
    high_confidence,
    is_check_candidate,
    match_row_rules,
    needs_autotag,
    render_diff_fractions,
    retag_topic,
    tick_tracker_chapter_rows,
    tick_tracker_rows,
    tracker_chapter_rows,
    tracker_topic_rows,
)


def _pdf(*, text: str | None = None, tagged: bool = False) -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    if text is not None:
        font = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type1,
                BaseFont=pikepdf.Name.Helvetica,
            )
        )
        page.obj["/Resources"] = pikepdf.Dictionary(F1=font)
        page.obj["/Resources"] = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        content = f"BT /F1 48 Tf 20 100 Td ({text}) Tj ET".encode()
        page.obj["/Contents"] = pdf.make_stream(content)
    if tagged:
        pdf.Root["/MarkInfo"] = pikepdf.Dictionary(Marked=True)
        pdf.Root["/StructTreeRoot"] = pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.StructTreeRoot, K=pikepdf.Array())
        )
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


def _adobe_report(failed: int, *, rules: list[tuple[str, str, str]] | None = None) -> bytes:
    rules = rules or [("Document", "Bookmarks", "Passed")]
    detailed: dict[str, list[dict[str, str]]] = {}
    for section, rule, status in rules:
        detailed.setdefault(section, []).append({"Rule": rule, "Status": status})
    return json.dumps(
        {"Summary": {"Failed": failed, "Passed": 1}, "Detailed Report": detailed}
    ).encode()


def _topic(**overrides: object) -> dict:
    topic = {
        "topicId": "t1",
        "status": "swept",
        "resultKind": "resolved",
        "residualCategories": {},
        "originalSha256": "a",
        "reSweptSha256": "b",
        "renderIdentical": True,
        "secondPassByteStable": True,
    }
    topic.update(overrides)
    return topic


PASS = {"pass": True, "readable": True, "failed": 0, "failedRules": [], "diagnostics": []}


def _rules(*entries: tuple[str, str]) -> list[dict[str, str]]:
    base = [
        ("Logical Reading Order", "Needs manual check"),
        ("Color contrast", "Needs manual check"),
        ("Other elements alternate text", "Passed"),
        ("Headers", "Passed"),
    ]
    return [{"section": "x", "rule": rule, "status": status} for rule, status in (*base, *entries)]


TRACKER = (
    "# Tracker\n\n## Per course\n\n### `trig`\n\nSheet \"Trig\".\n\n"
    "- [ ] ch `c1` row 2 **Chapter 0** chapter PDF fails: Other Elements [fork]\n"
    "  - [ ] topic `t1` row 6 Functions: Other Elements [fork]\n"
    "  - [ ] topic `t2` row 7 Tables: Headers [fork], Tab Order [fork] (in Aug 25 map)\n"
    "  - [x] topic `t3` row 8 Done: Headers [fork] (done 2026-09-01, by hand)\n"
    "  - [ ] topic `t4` Unlisted (no workbook row): Headers [fork]\n\n"
    "### `other`\n\n  - [ ] topic `t9` row 1 Elsewhere: Headers [fork]\n"
)


class AdobeGateTests(unittest.TestCase):
    def test_zero_failures_pass(self) -> None:
        gate = adobe_gate(_adobe_report(0))
        self.assertTrue(gate["pass"])
        self.assertEqual(gate["failed"], 0)

    def test_failed_rules_are_named(self) -> None:
        gate = adobe_gate(
            _adobe_report(1, rules=[("Tables", "Headers", "Failed"), ("Document", "Bookmarks", "Passed")])
        )
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["failedRules"], ["Tables: Headers"])

    def test_unreadable_report_never_passes(self) -> None:
        gate = adobe_gate(b"not json")
        self.assertFalse(gate["pass"])
        self.assertFalse(gate["readable"])


class DecisionTests(unittest.TestCase):
    def test_resolved_identical_and_api_pass_is_publishable(self) -> None:
        decision = decide_topic(_topic(), adobe=PASS, autotagged=False, max_render_diff=0.05)
        self.assertTrue(decision["publishable"])
        self.assertFalse(decision["renderException"])

    def test_residual_topic_is_held_with_the_category(self) -> None:
        topic = _topic(resultKind="residual", residualCategories={"Other Elements": "residual"})
        self.assertFalse(is_check_candidate(topic))
        decision = decide_topic(topic, adobe=PASS, autotagged=False, max_render_diff=0.05)
        self.assertFalse(decision["publishable"])
        self.assertIn("resultKind residual (Other Elements)", decision["reasons"])

    def test_table_only_residual_is_checked_and_defers_to_adobe(self) -> None:
        topic = _topic(
            resultKind="residual",
            residualCategories={"Tables Headers": "residual", "Tables Regularity": "residual"},
        )
        self.assertTrue(is_check_candidate(topic))
        passed = decide_topic(topic, adobe=PASS, autotagged=False, max_render_diff=0.05)
        self.assertTrue(passed["publishable"])
        self.assertTrue(passed["residualException"])
        self.assertTrue(passed["notes"][0].startswith("local audit residual"))
        held = decide_topic(topic, adobe=None, autotagged=False, max_render_diff=0.05)
        self.assertIn("resultKind residual (Tables Headers, Tables Regularity)", held["reasons"])
        mixed = _topic(
            resultKind="residual",
            residualCategories={"Tables Headers": "residual", "Other Elements": "residual"},
        )
        self.assertFalse(is_check_candidate(mixed))

    def test_orphan_block_residual_defers_to_adobe_but_is_not_high_confidence(self) -> None:
        topic = _topic(
            resultKind="residual",
            residualCategories={
                "Tables Headers": "resolved",
                "Other Elements Alternate Text": "residual",
            },
        )
        self.assertTrue(is_check_candidate(topic))
        passed = decide_topic(topic, adobe=PASS, autotagged=False, max_render_diff=0.05)
        self.assertTrue(passed["publishable"])
        self.assertTrue(passed["residualException"])
        held = decide_topic(topic, adobe=None, autotagged=False, max_render_diff=0.05)
        self.assertIn("resultKind residual (Other Elements Alternate Text)", held["reasons"])
        verdict = high_confidence(
            topic, PASS, row_rules=["Other Elements"], fonts_without_tounicode=0
        )
        self.assertFalse(verdict["high"])
        self.assertTrue(verdict["reasons"][0].startswith("pushed on a local"))

    def test_adobe_failure_holds_a_locally_resolved_topic(self) -> None:
        adobe = {**PASS, "pass": False, "failed": 2, "failedRules": ["Page Content: Other Elements"]}
        decision = decide_topic(_topic(), adobe=adobe, autotagged=False, max_render_diff=0.05)
        self.assertFalse(decision["publishable"])
        self.assertTrue(decision["reasons"][0].startswith("Adobe check failed 2"))

    def test_missing_adobe_check_holds(self) -> None:
        decision = decide_topic(_topic(), adobe=None, autotagged=False, max_render_diff=0.05)
        self.assertEqual(decision["reasons"], ["Adobe check not run"])

    def test_autotagged_within_threshold_gets_a_render_exception(self) -> None:
        decision = decide_topic(
            _topic(renderIdentical=False),
            adobe=PASS,
            autotagged=True,
            max_render_diff=0.05,
            max_render_diff_observed=0.01,
        )
        self.assertTrue(decision["publishable"])
        self.assertTrue(decision["renderException"])

    def test_autotagged_over_threshold_is_held(self) -> None:
        decision = decide_topic(
            _topic(renderIdentical=False),
            adobe=PASS,
            autotagged=True,
            max_render_diff=0.05,
            max_render_diff_observed=0.2,
        )
        self.assertFalse(decision["publishable"])
        self.assertIn("render diff 0.2000 above 0.0500", decision["reasons"])

    def test_render_change_without_autotag_is_held(self) -> None:
        decision = decide_topic(
            _topic(renderIdentical=False), adobe=PASS, autotagged=False, max_render_diff=0.05
        )
        self.assertIn("render changed without Auto-Tag", decision["reasons"])

    def test_unchanged_topic_is_not_a_check_candidate_and_is_held(self) -> None:
        topic = _topic(reSweptSha256="a")
        self.assertFalse(is_check_candidate(topic))
        decision = decide_topic(topic, adobe=None, autotagged=False, max_render_diff=0.05)
        self.assertIn("unchanged by the sweep", decision["reasons"])

    def test_needs_autotag_only_for_missing_structure_tree(self) -> None:
        self.assertTrue(
            needs_autotag(_topic(resultKind="residual", residualCategories={"Tagged Content": "residual"}))
        )
        self.assertFalse(
            needs_autotag(_topic(resultKind="residual", residualCategories={"Tables Headers": "residual"}))
        )
        self.assertFalse(
            needs_autotag(_topic(status="failed", residualCategories={"Tagged Content": "residual"}))
        )


class ManifestTests(unittest.TestCase):
    def test_publish_manifest_keeps_only_publishable_topics(self) -> None:
        manifest = {
            "runAt": "now",
            "pipelineFingerprint": "fp",
            "course": {"courseId": "c"},
            "topics": [_topic(topicId="keep"), _topic(topicId="hold")],
        }
        decisions = {"keep": {"publishable": True}, "hold": {"publishable": False}}
        result = build_publish_manifest(manifest, decisions, policy={"maxRenderDiff": 0.05})
        self.assertEqual(result["schemaVersion"], 2)
        self.assertEqual([t["topicId"] for t in result["topics"]], ["keep"])
        self.assertEqual(result["course"], {"courseId": "c"})
        self.assertEqual(result["autoPolicy"], {"maxRenderDiff": 0.05})


class RenderDiffTests(unittest.TestCase):
    def test_identical_pages_have_zero_diff(self) -> None:
        pdf = _pdf(text="Hello")
        self.assertEqual(render_diff_fractions(pdf, pdf), [0.0])

    def test_changed_text_is_measured(self) -> None:
        fractions = render_diff_fractions(_pdf(text="Hello"), _pdf(text="World"))
        self.assertGreater(fractions[0], 0.0)
        self.assertLess(fractions[0], 0.5)

    def test_page_count_mismatch_raises(self) -> None:
        pdf = pikepdf.new()
        pdf.add_blank_page(page_size=(200, 200))
        pdf.add_blank_page(page_size=(200, 200))
        two = io.BytesIO()
        pdf.save(two)
        with self.assertRaises(ValueError):
            render_diff_fractions(_pdf(), two.getvalue())


class RetagTests(unittest.TestCase):
    def test_retag_runs_sweeps_and_reports_render_diff(self) -> None:
        original = _pdf(text="Hello")
        tagged = _pdf(text="Hello", tagged=True)
        calls: list[bytes] = []

        def fake_autotag(pdf_bytes: bytes) -> tuple[bytes, bytes]:
            calls.append(pdf_bytes)
            return tagged, b'{"report": true}'

        repaired, details = retag_topic(
            original,
            autotag=fake_autotag,
            classify=lambda sweep: ("resolved", "swept", {"categories": {}}),
        )
        self.assertEqual(calls, [original])
        self.assertEqual(details["adobeAutotagReport"], b'{"report": true}')
        self.assertEqual(details["resultKind"], "resolved")
        self.assertEqual(details["renderDiffFractions"], [] if details["renderIdentical"] else [0.0])
        topic = apply_retag_to_topic(_topic(renderIdentical=False, resultKind="residual"), repaired, details)
        self.assertTrue(topic["autotagged"])
        self.assertEqual(topic["resultKind"], "resolved")
        self.assertEqual(topic["appliedRepairs"][0], "adobeAutotag")

    def test_retag_falls_back_to_normalized_input_once(self) -> None:
        original = _pdf(text="Hello")
        tagged = _pdf(text="Hello", tagged=True)
        seen: list[str] = []

        def fake_autotag(pdf_bytes: bytes) -> tuple[bytes, bytes]:
            seen.append("call")
            return (tagged if len(seen) == 2 else original), b""

        retag_topic(
            original,
            autotag=fake_autotag,
            normalize=lambda pdf_bytes: pdf_bytes,
            classify=lambda sweep: ("resolved", "swept", {"categories": {}}),
        )
        self.assertEqual(len(seen), 2)

    def test_retag_without_usable_tagging_raises(self) -> None:
        original = _pdf(text="Hello")
        with self.assertRaises(ValueError):
            retag_topic(
                original,
                autotag=lambda pdf_bytes: (original, b""),
                classify=lambda sweep: ("resolved", "swept", {"categories": {}}),
            )


class HighConfidenceTests(unittest.TestCase):
    def test_row_rules_match_adobe_rules_by_prefix(self) -> None:
        passed, unresolved = match_row_rules(
            ["Other Elements", "Headers", "Tab Order"], _rules(("Tab order", "Failed"))
        )
        self.assertEqual(passed, ["Other Elements", "Headers"])
        self.assertEqual(unresolved, ["Tab Order"])

    def test_identical_render_and_passed_row_rules_is_high(self) -> None:
        verdict = high_confidence(
            _topic(), {**PASS, "rules": _rules()}, row_rules=["Other Elements"], fonts_without_tounicode=0
        )
        self.assertEqual(verdict, {"high": True, "reasons": []})

    def test_each_missing_condition_is_named(self) -> None:
        adobe = {**PASS, "rules": _rules(("Tagged content", "Needs manual check"))}
        verdict = high_confidence(
            _topic(renderIdentical=False, autotagged=True),
            adobe,
            row_rules=["Tab Order"],
            fonts_without_tounicode=2,
        )
        self.assertFalse(verdict["high"])
        self.assertEqual(
            verdict["reasons"],
            [
                "render not byte-identical",
                "needs manual check: Tagged content",
                "row rules not passed: Tab Order",
                "2 font(s) without ToUnicode",
            ],
        )

    def test_no_tracker_row_or_failed_check_is_not_high(self) -> None:
        self.assertIn(
            "no tracker row",
            high_confidence(_topic(), {**PASS, "rules": _rules()}, row_rules=None, fonts_without_tounicode=0)["reasons"],
        )
        self.assertIn(
            "Adobe check did not pass",
            high_confidence(_topic(), None, row_rules=[], fonts_without_tounicode=0)["reasons"],
        )

    def test_font_count_ignores_standard_encodings(self) -> None:
        self.assertEqual(count_fonts_without_tounicode(_pdf(text="Hi")), 1)
        pdf = pikepdf.open(io.BytesIO(_pdf(text="Hi")))
        pdf.pages[0].Resources.Font.F1.Encoding = pikepdf.Name.WinAnsiEncoding
        out = io.BytesIO()
        pdf.save(out)
        self.assertEqual(count_fonts_without_tounicode(out.getvalue()), 0)


class ChapterConfidenceTests(unittest.TestCase):
    def test_passing_book_with_passed_row_rules_is_high(self) -> None:
        verdict = chapter_high_confidence(
            {**PASS, "rules": _rules()},
            row_rules=["Other Elements", "Headers"],
            fonts_without_tounicode=0,
        )
        self.assertEqual(verdict, {"high": True, "reasons": []})

    def test_a_font_without_tounicode_blocks_the_tick(self) -> None:
        verdict = chapter_high_confidence(
            {**PASS, "rules": _rules()},
            row_rules=["Headers"],
            fonts_without_tounicode=3,
        )
        self.assertEqual(verdict["reasons"], ["3 font(s) without ToUnicode"])

    def test_failed_rules_and_a_missing_row_are_named(self) -> None:
        gate = {
            "pass": False,
            "readable": True,
            "failed": 1,
            "failedRules": ["Document: Bookmarks"],
            "rules": _rules(("Bookmarks", "Failed")),
        }
        self.assertEqual(
            chapter_high_confidence(
                gate, row_rules=["Bookmarks"], fonts_without_tounicode=0
            )["reasons"],
            ["Adobe check did not pass: Document: Bookmarks"],
        )
        self.assertIn(
            "no tracker row",
            chapter_high_confidence(
                {**PASS, "rules": _rules()}, row_rules=None, fonts_without_tounicode=0
            )["reasons"],
        )

    def test_a_manual_check_beyond_the_always_manual_pair_blocks_the_tick(self) -> None:
        verdict = chapter_high_confidence(
            {**PASS, "rules": _rules(("Tagged content", "Needs manual check"))},
            row_rules=[],
            fonts_without_tounicode=0,
        )
        self.assertEqual(verdict["reasons"], ["needs manual check: Tagged content"])


class TrackerTests(unittest.TestCase):
    def test_topic_rows_are_scoped_to_the_course(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracker.md"
            path.write_text(TRACKER, encoding="utf-8")
            rows = tracker_topic_rows(path, "trig")
            self.assertEqual(sorted(rows), ["t1", "t2", "t3", "t4"])
            self.assertEqual(rows["t2"]["rules"], ["Headers", "Tab Order"])
            self.assertTrue(rows["t3"]["done"])
            self.assertFalse(rows["t1"]["done"])

    def test_tick_appends_a_done_note_and_skips_ticked_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracker.md"
            path.write_text(TRACKER, encoding="utf-8")
            ticked = tick_tracker_rows(
                path, "trig", {"t1": "auto", "t3": "auto", "t9": "auto"}, date="2026-09-11"
            )
            self.assertEqual(ticked, ["t1"])
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "  - [x] topic `t1` row 6 Functions: Other Elements [fork] (done 2026-09-11, auto)\n", text
            )
            self.assertIn("(done 2026-09-01, by hand)\n", text)
            self.assertIn("  - [ ] topic `t9`", text)
            self.assertIn("- [ ] ch `c1`", text)


    def test_row_without_a_workbook_number_is_read_and_ticked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracker.md"
            path.write_text(TRACKER, encoding="utf-8")
            self.assertEqual(tracker_topic_rows(path, "trig")["t4"]["rules"], ["Headers"])
            self.assertEqual(tick_tracker_rows(path, "trig", {"t4": "auto"}, date="2026-09-14"), ["t4"])
            self.assertIn(
                "  - [x] topic `t4` Unlisted (no workbook row): Headers [fork] (done 2026-09-14, auto)\n",
                path.read_text(encoding="utf-8"),
            )

    def test_chapter_rows_tick_without_touching_topic_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracker.md"
            path.write_text(TRACKER, encoding="utf-8")
            rows = tracker_chapter_rows(path, "trig")
            self.assertEqual(sorted(rows), ["c1"])
            self.assertEqual(rows["c1"]["rules"], ["Other Elements"])
            ticked = tick_tracker_chapter_rows(
                path, "trig", {"c1": "rebuilt", "c9": "rebuilt"}, date="2026-09-14"
            )
            self.assertEqual(ticked, ["c1"])
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "- [x] ch `c1` row 2 **Chapter 0** chapter PDF fails: Other Elements [fork]"
                " (done 2026-09-14, rebuilt)\n",
                text,
            )
            self.assertIn("  - [ ] topic `t1` row 6", text)

    def test_sentence_joins_the_course_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracker.md"
            path.write_text(
                "# Tracker\n\n### `calculus`\n\nSheet \"Calculus\". Never swept.\n\n- [ ] ch `1` row 2\n",
                encoding="utf-8",
            )
            self.assertEqual(append_tracker_note(path, "calculus", "Auto run 2026-09-11: ok."), "appended")
            lines = path.read_text(encoding="utf-8").split("\n")
            self.assertEqual(lines[4], 'Sheet "Calculus". Never swept. Auto run 2026-09-11: ok.')
            self.assertEqual(lines[6], "- [ ] ch `1` row 2")

    def test_missing_course_gets_a_new_section_in_id_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracker.md"
            path.write_text("# Tracker\n\n### `algebra`\n\nText.\n\n### `other`\n\nText.\n", encoding="utf-8")
            self.assertEqual(append_tracker_note(path, "calculus", "Auto run."), "created")
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "# Tracker\n\n### `algebra`\n\nText.\n\n### `calculus`\n\nAuto run.\n\n### `other`\n\nText.\n",
            )
            self.assertEqual(append_tracker_note(path, "zoology", "Auto run."), "created")
            self.assertTrue(path.read_text(encoding="utf-8").endswith("### `zoology`\n\nAuto run.\n"))


if __name__ == "__main__":
    unittest.main()
