"""Unattended per-course topic PDF repair: sweep, Auto-Tag, Adobe check, publish.

This module holds the decisions the `auto` workflow makes in place of a person.
The orchestration itself lives in scripts/auto_fix_course.py. Everything here
takes plain data or injected callables so the rules can be tested without S3
or Adobe.

A topic is publishable when every one of these holds:

- the sweep finished with resultKind "resolved" and a byte-stable second pass,
  or its only residual categories are the local table audit's (Tables Headers,
  Tables Regularity), which Acrobat is known to accept;
- the repaired file differs from the original (otherwise there is nothing to
  push);
- the Adobe PDF Services accessibility checker reports zero failed rules on
  the repaired file;
- the repaired file renders identically to the original, or it went through
  Adobe Auto-Tag and no page changed more than `max_render_diff` of its pixels.

Anything else goes to the review queue with the reasons it was held.

A pushed topic is "high confidence" when, on top of that, the render is
byte-identical, Adobe marks nothing "Needs manual check" beyond the two rules
it always marks that way, every rule named on the topic's tracker row passed,
and no font lacks a ToUnicode map (the case where desktop Acrobat is stricter
than the API). The orchestrator ticks those tracker rows itself; the rest wait
for the user's check on dev.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any, Callable

import pikepdf
import pymupdf

from lib.accessibility_course_workflow import (
    prepare_pdf,
    render_hashes,
    sha256_bytes,
    validate_pdf,
)
from lib.adobe_residual_report import parse_adobe_report
from lib.character_encoding_sweep import (
    _effective_page_fonts,
    _font_key,
    _font_tounicode_stream,
)
from lib.pdf_a11y_audit import audit_pdf_bytes


DEFAULT_MAX_RENDER_DIFF = 0.05
RENDER_DIFF_DPI = 72
# A pixel counts as changed when its grey level moves by more than this. Adobe
# repaints glyphs with slightly different anti-aliasing, which this ignores;
# a missing or moved glyph is well above it.
_CHANNEL_DELTA = 64
# Adobe reports these two rules as "Needs manual check" on every document.
ALWAYS_MANUAL_RULES = frozenset({"logicalreadingorder", "colorcontrast"})
# Simple fonts with one of these encodings map to Unicode without /ToUnicode.
_STANDARD_ENCODINGS = frozenset(
    {"/WinAnsiEncoding", "/MacRomanEncoding", "/StandardEncoding", "/MacExpertEncoding"}
)
TOPIC_ROW_RE = re.compile(
    r"^(?P<indent>\s*)- \[(?P<done>[ xX])\] topic `(?P<id>[^`]*)`"
    r"(?: row (?P<row>\d+))? (?P<rest>.*)$"
)
# A chapter row names the chapter book the lambda merges from those topics.
CHAPTER_ROW_RE = re.compile(
    r"^(?P<indent>\s*)- \[(?P<done>[ xX])\] ch `(?P<id>[^`]*)`"
    r"(?: row (?P<row>\d+))? (?P<rest>.*)$"
)
_ROW_RULE_RE = re.compile(r"([A-Z][A-Za-z ]*?) \[(?:fork|lambda|both)\]")


def _normalized(value: object) -> str:
    return "".join(str(value or "").split()).casefold()


def _grey(page: pymupdf.Page) -> pymupdf.Pixmap:
    return page.get_pixmap(dpi=RENDER_DIFF_DPI, colorspace=pymupdf.csGRAY, alpha=False)


def render_diff_fractions(original: bytes, candidate: bytes) -> list[float]:
    """Per-page fraction of pixels that changed between two PDFs.

    Raises ValueError when the page counts or page pixel sizes differ, since
    a comparison would be meaningless there.
    """
    before = pymupdf.open(stream=original, filetype="pdf")
    after = pymupdf.open(stream=candidate, filetype="pdf")
    try:
        if before.page_count != after.page_count:
            raise ValueError(
                f"page count differs: {before.page_count} vs {after.page_count}"
            )
        fractions: list[float] = []
        for index in range(before.page_count):
            pix_before = _grey(before[index])
            pix_after = _grey(after[index])
            if pix_before.width != pix_after.width or pix_before.height != pix_after.height:
                raise ValueError(f"page {index + 1} pixel dimensions differ")
            total = pix_before.width * pix_before.height
            if total == 0:
                fractions.append(0.0)
                continue
            changed = sum(
                1
                for a, b in zip(pix_before.samples, pix_after.samples)
                if a - b > _CHANNEL_DELTA or b - a > _CHANNEL_DELTA
            )
            fractions.append(changed / total)
        return fractions
    finally:
        before.close()
        after.close()


def adobe_gate(report_bytes: bytes) -> dict[str, Any]:
    """Reduce an Adobe checker report to a pass/fail decision with evidence."""
    report = parse_adobe_report(report_bytes)
    failed: int | None = None
    manual: int | None = None
    for name, value in report.summary_counts.items():
        if _normalized(name) == "failed":
            failed = value
        elif _normalized(name) == "needsmanualcheck":
            manual = value
    failed_rules = [
        f"{rule.section}: {rule.rule}"
        for rule in report.rules
        if _normalized(rule.status) == "failed"
    ]
    readable = bool(report.rules) and failed is not None
    return {
        "pass": readable and failed == 0 and not failed_rules,
        "readable": readable,
        "failed": failed,
        "needsManualCheck": manual,
        "failedRules": failed_rules,
        "rules": [
            {"section": rule.section, "rule": rule.rule, "status": rule.status}
            for rule in report.rules
        ],
        "diagnostics": list(report.parser_diagnostics),
    }


def count_fonts_without_tounicode(pdf_bytes: bytes) -> int:
    """Fonts used on any page that have no /ToUnicode and no standard encoding."""
    count = 0
    seen: set[object] = set()
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for _name, font in _effective_page_fonts(page):
                key = _font_key(font)
                if key in seen:
                    continue
                seen.add(key)
                if _font_tounicode_stream(font) is not None:
                    continue
                if str(font.get("/Subtype")) == "/Type3":
                    continue
                encoding = font.get("/Encoding")
                if isinstance(encoding, pikepdf.Name) and str(encoding) in _STANDARD_ENCODINGS:
                    continue
                count += 1
    return count


def match_row_rules(
    row_rules: list[str], adobe_rules: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Split a tracker row's rules into (passed, unresolved) against an Adobe report.

    A tracker rule such as "Other Elements" names the Adobe rule it is a
    prefix of ("Other elements alternate text"). A rule is passed when exactly
    one Adobe rule matches and its status is Passed.
    """
    passed: list[str] = []
    unresolved: list[str] = []
    for rule in row_rules:
        wanted = _normalized(rule)
        matches = [
            entry for entry in adobe_rules if _normalized(entry.get("rule")).startswith(wanted)
        ]
        if len(matches) == 1 and _normalized(matches[0].get("status")) == "passed":
            passed.append(rule)
        else:
            unresolved.append(rule)
    return passed, unresolved


def high_confidence(
    topic: dict[str, Any],
    adobe: dict[str, Any] | None,
    *,
    row_rules: list[str] | None,
    fonts_without_tounicode: int,
) -> dict[str, Any]:
    """Decide whether a pushed topic's tracker row can be ticked without a person.

    Returns {"high": bool, "reasons": [...]} where reasons say what is missing.
    `row_rules` is None when the tracker has no row for the topic.
    """
    reasons: list[str] = []
    if row_rules is None:
        reasons.append("no tracker row")
    if topic.get("renderIdentical") is not True or topic.get("autotagged"):
        reasons.append("render not byte-identical")
    if adobe is None or not adobe.get("pass"):
        reasons.append("Adobe check did not pass")
    else:
        manual = sorted(
            entry["rule"]
            for entry in adobe.get("rules") or []
            if _normalized(entry.get("status")) == "needsmanualcheck"
            and _normalized(entry.get("rule")) not in ALWAYS_MANUAL_RULES
        )
        if manual:
            reasons.append("needs manual check: " + ", ".join(manual))
        if row_rules:
            _passed, unresolved = match_row_rules(row_rules, adobe.get("rules") or [])
            if unresolved:
                reasons.append("row rules not passed: " + ", ".join(unresolved))
    if fonts_without_tounicode:
        reasons.append(f"{fonts_without_tounicode} font(s) without ToUnicode")
    return {"high": not reasons, "reasons": reasons}


def chapter_high_confidence(
    adobe: dict[str, Any] | None,
    *,
    row_rules: list[str] | None,
    fonts_without_tounicode: int,
) -> dict[str, Any]:
    """Decide whether a rebuilt chapter book's tracker row can be ticked.

    Shaped like `high_confidence`, minus the render test: the book is merged by
    `generate-pdf-lambda` from the pushed previews, not repaired here, so there
    is nothing local to compare it against, and the rebuild is meant to change
    the file. The font test stays, because a font with no ToUnicode map is the
    known case where the API passes Character encoding and desktop Acrobat
    fails it, and the merged book carries the previews' fonts.
    """
    reasons: list[str] = []
    if row_rules is None:
        reasons.append("no tracker row")
    if adobe is None or not adobe.get("pass"):
        reasons.append("Adobe check did not pass")
        if adobe and adobe.get("failedRules"):
            reasons[-1] += ": " + ", ".join(adobe["failedRules"])
    else:
        manual = sorted(
            entry["rule"]
            for entry in adobe.get("rules") or []
            if _normalized(entry.get("status")) == "needsmanualcheck"
            and _normalized(entry.get("rule")) not in ALWAYS_MANUAL_RULES
        )
        if manual:
            reasons.append("needs manual check: " + ", ".join(manual))
        if row_rules:
            _passed, unresolved = match_row_rules(row_rules, adobe.get("rules") or [])
            if unresolved:
                reasons.append("row rules not passed: " + ", ".join(unresolved))
    if fonts_without_tounicode:
        reasons.append(f"{fonts_without_tounicode} font(s) without ToUnicode")
    return {"high": not reasons, "reasons": reasons}


def needs_autotag(topic: dict[str, Any]) -> bool:
    """True when the sweep left the topic with no structure tree."""
    categories = topic.get("residualCategories") or {}
    return categories.get("Tagged Content") == "residual" and topic.get(
        "status"
    ) not in {None, "failed"}


def retag_topic(
    original: bytes,
    *,
    autotag: Callable[[bytes], tuple[bytes, bytes]],
    normalize: Callable[[bytes], bytes] | None = None,
    classify: Callable[[dict[str, Any]], tuple[str, str, dict[str, Any]]],
    document_title: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Auto-Tag an untagged PDF, then run the local sweeps on the result.

    `autotag` returns (tagged_pdf, adobe_report). When the first attempt comes
    back without usable tagging and `normalize` is given, the input is rewritten
    with it and sent once more. `classify` maps a prepare_pdf result to
    (resultKind, status, residualDiagnostics).
    """
    original_validation = validate_pdf(original)
    attempts: list[dict[str, Any]] = []
    tagged: bytes | None = None
    adobe_report = b""
    for attempt, transform in (("plain", None), ("normalized", normalize)):
        if transform is None and attempt == "normalized":
            break
        source = transform(original) if transform else original
        try:
            candidate, adobe_report = autotag(source)
        except Exception as error:
            attempts.append({"attempt": attempt, "error": str(error)})
            continue
        audit = audit_pdf_bytes(candidate).to_dict()
        usable = bool(audit["struct_tree_root_present"] and audit["marked"])
        attempts.append({"attempt": attempt, "usableTagging": usable})
        if usable:
            tagged = candidate
            break
    if tagged is None:
        raise ValueError(f"Adobe Auto-Tag returned no usable tagging: {attempts}")
    if validate_pdf(tagged)["pages"] != original_validation["pages"]:
        raise ValueError("Adobe Auto-Tag changed the page count")

    repaired, sweep = prepare_pdf(tagged, document_title=document_title)
    repaired_validation = validate_pdf(repaired)
    if repaired_validation["pages"] != original_validation["pages"]:
        raise ValueError("Local sweeps changed the page count after Auto-Tag")
    repaired_audit = audit_pdf_bytes(repaired).to_dict()
    if not repaired_audit["struct_tree_root_present"] or not repaired_audit["marked"]:
        raise ValueError("Final PDF lost its tagging")
    result_kind, status, diagnostics = classify(sweep)
    render_identical = render_hashes(repaired) == render_hashes(original)
    diff_fractions = [] if render_identical else render_diff_fractions(original, repaired)
    details = {
        "autotagAttempts": attempts,
        "adobeAutotagReport": adobe_report,
        "originalValidation": original_validation,
        "reSweptValidation": repaired_validation,
        "renderIdentical": render_identical,
        "renderDiffFractions": diff_fractions,
        "maxRenderDiff": max(diff_fractions) if diff_fractions else 0.0,
        "resultKind": result_kind,
        "status": status,
        "residualDiagnostics": diagnostics,
        "sweep": sweep,
    }
    return repaired, details


# The local table audit is stricter than Acrobat: it flags rows of unequal
# width and headerless data cells that Acrobat's Tables rules accept. When
# these are the only residual categories, the Adobe report decides.
TABLE_CATEGORIES = frozenset({"Tables Headers", "Tables Regularity"})


def held_categories(topic: dict[str, Any]) -> list[str]:
    categories = topic.get("residualCategories") or {}
    return sorted(name for name, status in categories.items() if status != "resolved")


def table_only_residual(topic: dict[str, Any]) -> bool:
    """True when the sweep's only complaint is the local table audit."""
    held = held_categories(topic)
    return (
        topic.get("resultKind") == "residual"
        and bool(held)
        and set(held) <= TABLE_CATEGORIES
    )


def decide_topic(
    topic: dict[str, Any],
    *,
    adobe: dict[str, Any] | None,
    autotagged: bool,
    max_render_diff: float,
    max_render_diff_observed: float | None = None,
) -> dict[str, Any]:
    """Return {"publishable", "reasons", "notes", "renderException", "residualException"}.

    `residualException` marks a table-only residual that Adobe passed; the
    publisher needs it as an approved exception because the manifest entry
    still says residual.
    """
    reasons: list[str] = []
    notes: list[str] = []
    residual_override = False
    if topic.get("status") == "failed":
        reasons.append("sweep failed: " + "; ".join(topic.get("errors") or ["unknown"]))
    if topic.get("resultKind") != "resolved":
        held = held_categories(topic)
        if table_only_residual(topic) and adobe is not None and adobe.get("pass"):
            residual_override = True
            notes.append(
                f"local table audit residual ({', '.join(held)}) overridden: "
                "Adobe passes both Tables rules"
            )
        else:
            reasons.append(
                f"resultKind {topic.get('resultKind')}"
                + (f" ({', '.join(held)})" if held else "")
            )
    if topic.get("secondPassByteStable") is not True:
        reasons.append("second pass not byte-stable")
    if topic.get("reSweptSha256") and topic.get("reSweptSha256") == topic.get(
        "originalSha256"
    ):
        reasons.append("unchanged by the sweep")
    render_exception = False
    if topic.get("renderIdentical") is not True:
        if not autotagged:
            reasons.append("render changed without Auto-Tag")
        elif max_render_diff_observed is None:
            reasons.append("render diff not measured")
        elif max_render_diff_observed > max_render_diff:
            reasons.append(
                f"render diff {max_render_diff_observed:.4f} above {max_render_diff:.4f}"
            )
        else:
            render_exception = True
    if adobe is None:
        reasons.append("Adobe check not run")
    elif not adobe.get("readable"):
        reasons.append("Adobe report unreadable: " + "; ".join(adobe.get("diagnostics") or []))
    elif not adobe.get("pass"):
        reasons.append(
            f"Adobe check failed {adobe.get('failed')} rule(s): "
            + ", ".join(adobe.get("failedRules") or [])
        )
    return {
        "publishable": not reasons,
        "reasons": reasons,
        "notes": notes,
        "renderException": render_exception and not reasons,
        "residualException": residual_override and not reasons,
    }


def is_check_candidate(topic: dict[str, Any]) -> bool:
    """Topics worth an Adobe API call: resolved (or table-only residual), stable, changed."""
    return (
        topic.get("status") != "failed"
        and (topic.get("resultKind") == "resolved" or table_only_residual(topic))
        and topic.get("secondPassByteStable") is True
        and bool(topic.get("reSweptSha256"))
        and topic.get("reSweptSha256") != topic.get("originalSha256")
    )


def build_publish_manifest(
    manifest: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
    *,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """A schema-2 manifest holding only the publishable topics."""
    topics = [
        topic
        for topic in manifest.get("topics") or []
        if decisions.get(str(topic.get("topicId")), {}).get("publishable")
    ]
    return {
        "schemaVersion": 2,
        "runAt": manifest.get("runAt"),
        "pipelineFingerprint": manifest.get("pipelineFingerprint"),
        "course": manifest.get("course"),
        "autoPolicy": policy,
        "sourceManifest": manifest.get("sourceManifest"),
        "topics": topics,
    }


def apply_retag_to_topic(
    topic: dict[str, Any],
    repaired: bytes,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Return a manifest topic updated with the Auto-Tag result."""
    categories = {
        name: value.get("status")
        for name, value in (details["residualDiagnostics"].get("categories") or {}).items()
        if isinstance(value, dict)
    }
    return {
        **topic,
        "reSweptSha256": sha256_bytes(repaired),
        "reSweptBytes": len(repaired),
        "pages": details["reSweptValidation"]["pages"],
        "status": details["status"],
        "resultKind": details["resultKind"],
        "appliedRepairs": ["adobeAutotag", *details["sweep"].get("appliedRepairs", [])],
        "warnings": details["sweep"].get("warnings") or [],
        "residualCategories": categories,
        "renderIdentical": details["renderIdentical"],
        "secondPassByteStable": (details["sweep"].get("secondPass") or {}).get(
            "byteStable"
        ),
        "secondPassExecuted": (details["sweep"].get("secondPass") or {}).get(
            "executed"
        ),
        "autotagged": True,
        "maxRenderDiff": details["maxRenderDiff"],
    }


def append_tracker_note(tracker: Path, course_id: str, sentence: str) -> str:
    """Add a sentence to the paragraph under the course heading.

    Returns "appended" when the course paragraph gained the sentence, or
    "created" when the file had no heading for the course and a new section
    was inserted in course-id order among the existing `###` headings (at the
    end when it sorts last). The file is created when missing.
    """
    heading = f"### `{course_id}`"
    if tracker.exists():
        lines = tracker.read_text(encoding="utf-8").split("\n")
    else:
        lines = ["# PDF accessibility progress tracker", ""]
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        insert_at = next(
            (
                i
                for i, line in enumerate(lines)
                if re.match(r"^### `(.+)`\s*$", line)
                and re.match(r"^### `(.+)`\s*$", line).group(1) > course_id
            ),
            None,
        )
        if insert_at is None:
            while lines and lines[-1].strip() == "":
                lines.pop()
            lines.extend(["", heading, "", sentence, ""])
        else:
            lines[insert_at:insert_at] = [heading, "", sentence, ""]
        tracker.write_text("\n".join(lines), encoding="utf-8")
        return "created"
    index = start + 1
    while index < len(lines) and lines[index].strip() == "":
        index += 1
    if index >= len(lines) or re.match(r"^(#|- \[|\s*- \[)", lines[index]):
        lines.insert(index, sentence)
        lines.insert(index + 1, "")
    else:
        lines[index] = lines[index].rstrip() + " " + sentence
    tracker.write_text("\n".join(lines), encoding="utf-8")
    return "appended"


def _course_section(lines: list[str], course_id: str) -> tuple[int, int] | None:
    heading = f"### `{course_id}`"
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return None
    end = start + 1
    while end < len(lines) and not re.match(r"^#{2,3} ", lines[end]):
        end += 1
    return start, end


def _tracker_rows(
    tracker: Path, course_id: str, pattern: re.Pattern[str]
) -> dict[str, dict[str, Any]]:
    if not tracker.exists():
        return {}
    lines = tracker.read_text(encoding="utf-8").split("\n")
    section = _course_section(lines, course_id)
    if section is None:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for index in range(*section):
        match = pattern.match(lines[index])
        if not match:
            continue
        rows[match.group("id")] = {
            "line": index,
            "done": match.group("done").lower() == "x",
            "rules": _ROW_RULE_RE.findall(match.group("rest")),
        }
    return rows


def tracker_topic_rows(tracker: Path, course_id: str) -> dict[str, dict[str, Any]]:
    """Topic rows under the course heading, keyed by topic id.

    Each value holds the line index, whether the row is ticked, and the rules
    named on the row ("Other Elements", "Headers", ...).
    """
    return _tracker_rows(tracker, course_id, TOPIC_ROW_RE)


def tracker_chapter_rows(tracker: Path, course_id: str) -> dict[str, dict[str, Any]]:
    """Chapter rows under the course heading, keyed by chapter id.

    Shaped like `tracker_topic_rows`, over the `- [ ] ch` lines.
    """
    return _tracker_rows(tracker, course_id, CHAPTER_ROW_RE)


def _tick_rows(
    tracker: Path,
    course_id: str,
    notes: dict[str, str],
    *,
    date: str,
    kind: str,
    pattern: re.Pattern[str],
) -> list[str]:
    rows = _tracker_rows(tracker, course_id, pattern)
    lines = tracker.read_text(encoding="utf-8").split("\n")
    ticked: list[str] = []
    for row_id, note in notes.items():
        row = rows.get(row_id)
        if row is None or row["done"]:
            continue
        line = lines[row["line"]]
        line = line.replace(f"- [ ] {kind}", f"- [x] {kind}", 1).rstrip()
        lines[row["line"]] = f"{line} (done {date}, {note})"
        ticked.append(row_id)
    if ticked:
        tracker.write_text("\n".join(lines), encoding="utf-8")
    return ticked


def tick_tracker_rows(
    tracker: Path, course_id: str, notes: dict[str, str], *, date: str
) -> list[str]:
    """Tick the given topic rows and append `(done <date>, <note>)` to each.

    Rows already ticked are left alone. Returns the ids that were ticked.
    """
    return _tick_rows(
        tracker, course_id, notes, date=date, kind="topic", pattern=TOPIC_ROW_RE
    )


def tick_tracker_chapter_rows(
    tracker: Path, course_id: str, notes: dict[str, str], *, date: str
) -> list[str]:
    """Tick the given chapter rows, the same way as topic rows."""
    return _tick_rows(
        tracker, course_id, notes, date=date, kind="ch", pattern=CHAPTER_ROW_RE
    )
