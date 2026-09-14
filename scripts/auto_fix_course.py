#!/usr/bin/env python3
"""Repair every topic PDF of one course and push the passing ones to dev.

Stages, each recorded in reports/<course>-auto-<timestamp>.auto.json:

1. sweep the topics with open rows in the progress tracker for the course, or
   every topic in the default TOC with --all-topics (scripts/sweep_accessibility_issue_map.py
   --all-topics);
2. Adobe Auto-Tag any topic the sweep left without a structure tree, then sweep
   the tagged file;
3. run the Adobe PDF Services accessibility checker on every changed, resolved
   topic;
4. write a manifest holding only the topics that pass the policy in
   lib/auto_course_pipeline.py, plus a review queue for the rest;
5. publish that manifest through the fail-closed replacer unless --plan-only;
6. tick the tracker rows of pushed topics that meet the high-confidence bar;
7. on dev, after a push, invoke `generate-pdf-dev-generateCoursePdf` so the
   topic downloads and every TOC's chapter books are rebuilt from the pushed
   previews, wait for the chapter books to land, invalidate the course's
   `topic_pdfs/*` and `worksheets/*` on CloudFront, run the Adobe checker on the
   rebuilt books and tick the chapter rows that pass;
8. append a dated sentence to the course paragraph in the progress tracker and
   re-render the HTML progress page from it.

`--rebuild-only` runs stage 7 by itself against whatever is already on dev,
for a course whose topics were pushed by an earlier run.

Nothing here asks a person anything. The exit code is 0 when the run completed,
even with held topics or a chapter rebuild that failed after the push; 2 when
the publish stage raised, or when --rebuild-only could not start the rebuild.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import render_accessibility_progress as progress_page  # noqa: E402
import sweep_accessibility_issue_map as sweep  # noqa: E402
from lib.adobe_autotag import (  # noqa: E402
    autotag_pdf_from_secret,
    check_pdf_accessibility_from_secret,
    normalize_pdf_for_autotag,
)
from lib.auto_course_pipeline import (  # noqa: E402
    DEFAULT_MAX_RENDER_DIFF,
    adobe_gate,
    append_tracker_note,
    apply_retag_to_topic,
    build_publish_manifest,
    chapter_high_confidence,
    count_fonts_without_tounicode,
    decide_topic,
    high_confidence,
    is_check_candidate,
    needs_autotag,
    retag_topic,
    tick_tracker_chapter_rows,
    tick_tracker_rows,
    tracker_chapter_rows,
    tracker_topic_rows,
)
from lib.channels_paths import (  # noqa: E402
    default_toc_id,
    load_course_json,
    preview_key,
)
from lib.cloudfront import invalidate_paths  # noqa: E402
from lib.config import channels_bucket, cloudfront_distribution_id  # noqa: E402
from lib.course_rebuild import (  # noqa: E402
    course_pdf_function_name,
    dispatch_warning,
    invoke_course_rebuild,
    object_state,
    rebuild_dispatched,
    snapshot_worksheets,
    wait_for_worksheets,
    worksheet_keys,
)
from lib.s3_pdf_replacement import manifest_sha256, publish_manifest  # noqa: E402

ROOT = SCRIPT_DIR.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("course_id")
    parser.add_argument("--env", choices=("dev", "prod"), default="dev")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--max-render-diff",
        type=float,
        default=DEFAULT_MAX_RENDER_DIFF,
        help=(
            "Largest per-page fraction of changed pixels accepted for an "
            "Auto-Tagged topic (default %(default)s)"
        ),
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Run every stage but replace nothing in S3 (publish runs in plan mode)",
    )
    parser.add_argument(
        "--skip-autotag",
        action="store_true",
        help="Hold untagged topics for review instead of calling Adobe Auto-Tag",
    )
    parser.add_argument(
        "--check-unchanged",
        action="store_true",
        help="Also run the Adobe checker on topics the sweep did not change",
    )
    parser.add_argument(
        "--skip-rebuild",
        action="store_true",
        help=(
            "Do not invoke generateCoursePdf after the push, so the topic "
            "downloads and chapter books on dev keep predating it"
        ),
    )
    parser.add_argument(
        "--rebuild-timeout",
        type=float,
        default=2700.0,
        help=(
            "Seconds to wait for the rebuilt chapter books before giving up "
            "(default %(default)s; a book that runs past the chapter lambda's "
            "clock is re-driven by Lambda's async retries)"
        ),
    )
    parser.add_argument(
        "--rebuild-poll",
        type=float,
        default=60.0,
        help="Seconds between S3 checks while waiting for the books (default %(default)s)",
    )
    parser.add_argument(
        "--rebuild-only",
        action="store_true",
        help=(
            "Run stage 7 alone against what is already on dev: no sweep, no "
            "push, just the rebuild, the worksheet check and the chapter ticks"
        ),
    )
    parser.add_argument(
        "--check-all-worksheets",
        action="store_true",
        help=(
            "Adobe-check every rebuilt chapter book, not only the ones whose "
            "tracker row is still open"
        ),
    )
    parser.add_argument(
        "--tracker",
        default="reports/ACCESSIBILITY_PROGRESS.md",
        help="Progress tracker to append the run sentence to; empty string skips",
    )
    parser.add_argument(
        "--progress-html",
        default="reports/accessibility-progress.html",
        help="HTML page re-rendered from the tracker after the note; empty string skips",
    )
    parser.add_argument("--output-root", default="pdfs/accessibility-issue-map")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--resume", action="store_true", help="Pass --resume to the sweep")
    parser.add_argument(
        "--all-topics",
        action="store_true",
        help="Sweep every default-TOC topic instead of only the open tracker rows",
    )
    return parser.parse_args()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _classify(sweep_result: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    diagnostics = sweep.build_residual_diagnostics(sweep_result, None, all_categories=True)
    result_kind, status = sweep.classify_residual_status(
        diagnostics, sweep_result.get("warnings")
    )
    return result_kind, status, diagnostics


def open_topic_ids(tracker: str, course_id: str) -> list[str] | None:
    """Topic ids with unticked rows under the course heading, or None to sweep all.

    None is returned when no tracker is configured; an empty list means the
    tracker has no open topic rows for the course.
    """
    if not tracker:
        return None
    rows = tracker_topic_rows(Path(tracker), course_id)
    return sorted(topic_id for topic_id, row in rows.items() if not row["done"])


def run_sweep(
    args: argparse.Namespace, stem: Path, topic_ids: list[str] | None
) -> tuple[Path, int]:
    report = stem.with_suffix(".sweep.json")
    manifest = stem.with_suffix(".sweep.manifest.json")
    command = [
        sys.executable,
        str(SCRIPT_DIR / "sweep_accessibility_issue_map.py"),
        "--course-id",
        args.course_id,
        "--all-topics",
        "--env",
        args.env,
        "--workers",
        str(args.workers),
        "--output-root",
        args.output_root,
        "--report",
        str(report),
        "--manifest",
        str(manifest),
    ]
    for topic_id in topic_ids or []:
        command.extend(["--topic-id", topic_id])
    if args.resume:
        command.append("--resume")
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if not manifest.is_file():
        raise RuntimeError(f"sweep wrote no manifest (exit {completed.returncode})")
    return manifest, completed.returncode


def _autotag_one(topic: dict[str, Any]) -> tuple[dict[str, Any], bytes | None, dict[str, Any] | None, str | None]:
    original = (ROOT / str(topic["originalPdf"])).read_bytes()
    try:
        repaired, details = retag_topic(
            original,
            autotag=autotag_pdf_from_secret,
            normalize=normalize_pdf_for_autotag,
            classify=_classify,
            document_title=topic.get("topicTitle"),
        )
    except Exception as error:
        return topic, None, None, str(error)
    return topic, repaired, details, None


def stage_autotag(
    topics: list[dict[str, Any]],
    *,
    workers: int,
    skip: bool,
    diagnostics_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return the topic list with Auto-Tag results folded in, and a log."""
    log: dict[str, dict[str, Any]] = {}
    candidates = [topic for topic in topics if needs_autotag(topic)]
    if skip or not candidates:
        for topic in candidates:
            log[str(topic["topicId"])] = {"skipped": True}
        return topics, log
    by_id = {str(topic["topicId"]): topic for topic in topics}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_autotag_one, topic) for topic in candidates]
        for future in as_completed(futures):
            topic, repaired, details, error = future.result()
            topic_id = str(topic["topicId"])
            if error or repaired is None or details is None:
                log[topic_id] = {"error": error}
                by_id[topic_id] = {
                    **topic,
                    "warnings": [*(topic.get("warnings") or []), f"autotag failed: {error}"],
                }
                continue
            swept_path = ROOT / str(topic["reSweptPdf"])
            swept_path.parent.mkdir(parents=True, exist_ok=True)
            swept_path.write_bytes(repaired)
            report_path = diagnostics_dir / f"{topic_id}.autotag-report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_bytes(details.pop("adobeAutotagReport") or b"")
            _write_json(diagnostics_dir / f"{topic_id}.autotag.json", details)
            by_id[topic_id] = apply_retag_to_topic(topic, repaired, details)
            log[topic_id] = {
                "resultKind": details["resultKind"],
                "maxRenderDiff": details["maxRenderDiff"],
                "attempts": details["autotagAttempts"],
                "report": str(report_path),
            }
    return [by_id[str(topic["topicId"])] for topic in topics], log


def _check_one(topic: dict[str, Any], out_dir: Path) -> tuple[str, dict[str, Any]]:
    topic_id = str(topic["topicId"])
    pdf_bytes = (ROOT / str(topic["reSweptPdf"])).read_bytes()
    last_error: str | None = None
    for _attempt in range(2):
        try:
            report = check_pdf_accessibility_from_secret(pdf_bytes)
        except Exception as error:
            last_error = str(error)
            continue
        path = out_dir / f"{topic_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(report)
        gate = adobe_gate(report)
        gate["report"] = str(path)
        return topic_id, gate
    return topic_id, {
        "pass": False,
        "readable": False,
        "failed": None,
        "needsManualCheck": None,
        "failedRules": [],
        "diagnostics": [f"checker call failed: {last_error}"],
        "report": None,
    }


def stage_adobe_check(
    topics: list[dict[str, Any]],
    *,
    workers: int,
    include_unchanged: bool,
    out_dir: Path,
) -> dict[str, dict[str, Any]]:
    def wanted(topic: dict[str, Any]) -> bool:
        if is_check_candidate(topic):
            return True
        return (
            include_unchanged
            and topic.get("status") != "failed"
            and topic.get("resultKind") == "resolved"
        )

    selected = [topic for topic in topics if wanted(topic)]
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_check_one, topic, out_dir) for topic in selected]
        for future in as_completed(futures):
            topic_id, gate = future.result()
            results[topic_id] = gate
    return results


def stage_tick_rows(
    tracker: Path,
    course_id: str,
    topics: list[dict[str, Any]],
    checks: dict[str, dict[str, Any]],
    publish_report: dict[str, Any],
    *,
    date: str,
    audit: str,
) -> dict[str, Any]:
    """Tick the tracker rows of pushed topics that meet the high-confidence bar.

    Returns {"ticked": [...ids], "notTicked": {id: [reasons]}} over the topics
    the publish report marks verified.
    """
    verified = {
        str(entry.get("topicId"))
        for entry in publish_report.get("objects") or []
        if entry.get("status") in {"verified", "already-applied"}
    }
    rows = tracker_topic_rows(tracker, course_id)
    notes: dict[str, str] = {}
    not_ticked: dict[str, list[str]] = {}
    for topic in topics:
        topic_id = str(topic.get("topicId"))
        if topic_id not in verified:
            continue
        row = rows.get(topic_id)
        try:
            fonts = count_fonts_without_tounicode(
                (ROOT / str(topic["reSweptPdf"])).read_bytes()
            )
        except Exception as error:
            not_ticked[topic_id] = [f"font check failed: {error}"]
            continue
        verdict = high_confidence(
            topic,
            checks.get(topic_id),
            row_rules=None if row is None else row["rules"],
            fonts_without_tounicode=fonts,
        )
        if verdict["high"]:
            notes[topic_id] = (
                "auto run pushed to dev, Adobe API passed every row rule, "
                f"render identical, audit {audit}"
            )
        else:
            not_ticked[topic_id] = verdict["reasons"]
    ticked = tick_tracker_rows(tracker, course_id, notes, date=date)
    for topic_id in notes:
        if topic_id not in ticked:
            not_ticked[topic_id] = ["row already ticked"]
    return {"ticked": ticked, "notTicked": not_ticked}


def _lambda_client():
    """A Lambda client that will not re-invoke generateCoursePdf on its own.

    generateCoursePdf has a 120s timeout and botocore's default read timeout is
    60s with four retries, so a slow fan-out would otherwise dispatch several
    full rebuilds.
    """
    return boto3.client(
        "lambda", config=Config(read_timeout=150, retries={"max_attempts": 0})
    )


def stage_rebuild(
    args: argparse.Namespace,
    s3_client,
    lambda_client,
    *,
    bucket: str,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Rebuild the course's downloads and chapter books on dev, then wait for them.

    The chapter books are what the tracker's chapter rows describe, and nothing
    a user downloads changes until this runs: the UI serves the lambda's wraps,
    not the previews this pipeline repairs.

    One invocation rebuilds every TOC of the course. The wait covers the
    default TOC alone, because that is the TOC the tracker's chapter ids name.
    """
    course = load_course_json(s3_client, bucket, args.course_id)
    toc_id = default_toc_id(course)
    seen_previews: dict[str, bool] = {}

    def has_preview(topic_id: str) -> bool:
        if topic_id not in seen_previews:
            key = preview_key(args.course_id, topic_id)
            seen_previews[topic_id] = object_state(s3_client, bucket, key) is not None
        return seen_previews[topic_id]

    keys = worksheet_keys(course, args.course_id, toc_id, has_preview=has_preview)
    stage: dict[str, Any] = {
        "tocId": toc_id,
        "expectedWorksheets": keys,
        "functionName": course_pdf_function_name(args.env),
    }
    if not keys:
        stage["skipped"] = "the default TOC has no chapter with a PDF-bearing topic"
        return stage
    baseline = snapshot_worksheets(s3_client, bucket, keys)
    stage["missingBefore"] = sorted(
        chapter_id for chapter_id, state in baseline.items() if state is None
    )
    try:
        stage["invocation"] = invoke_course_rebuild(
            lambda_client, args.course_id, function_name=stage["functionName"]
        )
    except Exception as error:
        stage["error"] = f"invoke failed: {error}"
        return stage
    if not rebuild_dispatched(stage["invocation"]):
        stage["error"] = "generateCoursePdf did not report every chapter book dispatched"
        return stage
    warning = dispatch_warning(stage["invocation"])
    if warning:
        stage["dispatchWarning"] = warning
    # The wait runs for up to three quarters of an hour, so record the
    # invocation before it starts rather than only once it ends.
    if checkpoint is not None:
        checkpoint(stage)

    def heartbeat(done: int, total: int, elapsed: float) -> None:
        print(f"rebuilt {done}/{total} chapter books after {elapsed:.0f}s", file=sys.stderr)

    stage["wait"] = wait_for_worksheets(
        s3_client,
        bucket,
        keys,
        baseline,
        timeout=args.rebuild_timeout,
        poll=args.rebuild_poll,
        on_poll=heartbeat,
    )
    # The lambda invalidates worksheets/* when it dispatches, minutes before
    # the books are written, and it never invalidates the topic wraps. Both
    # need clearing now that the objects are in place.
    paths = [
        f"/courses/{args.course_id}/topic_pdfs/*",
        f"/courses/{args.course_id}/worksheets/*",
    ]
    try:
        stage["cloudFront"] = {
            "requestedPaths": paths,
            "invalidationId": invalidate_paths(cloudfront_distribution_id(args.env), paths),
        }
    except Exception as error:
        stage["cloudFront"] = {"requestedPaths": paths, "error": str(error)}
    return stage


def _check_worksheet_one(
    chapter_id: str, key: str, s3_client, bucket: str, out_dir: Path
) -> tuple[str, dict[str, Any]]:
    try:
        pdf_bytes = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as error:
        return chapter_id, {"key": key, "pass": False, "error": f"download failed: {error}"}
    last_error: str | None = None
    for _attempt in range(2):
        try:
            report = check_pdf_accessibility_from_secret(pdf_bytes)
        except Exception as error:
            last_error = str(error)
            continue
        path = out_dir / f"{chapter_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(report)
        gate = adobe_gate(report)
        gate["key"] = key
        gate["report"] = str(path)
        gate["fontsWithoutToUnicode"] = count_fonts_without_tounicode(pdf_bytes)
        return chapter_id, gate
    return chapter_id, {
        "key": key,
        "pass": False,
        "readable": False,
        "failedRules": [],
        "diagnostics": [f"checker call failed: {last_error}"],
        "report": None,
        "fontsWithoutToUnicode": count_fonts_without_tounicode(pdf_bytes),
    }


def stage_worksheet_check(
    rebuilt: dict[str, str],
    s3_client,
    *,
    bucket: str,
    workers: int,
    out_dir: Path,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(_check_worksheet_one, chapter_id, key, s3_client, bucket, out_dir)
            for chapter_id, key in rebuilt.items()
        ]
        for future in as_completed(futures):
            chapter_id, gate = future.result()
            results[chapter_id] = gate
    return results


def stage_tick_chapter_rows(
    tracker: Path,
    course_id: str,
    checks: dict[str, dict[str, Any]],
    *,
    date: str,
    reports: str,
) -> dict[str, Any]:
    """Tick the chapter rows whose rebuilt book passes every rule the row names."""
    rows = tracker_chapter_rows(tracker, course_id)
    notes: dict[str, str] = {}
    not_ticked: dict[str, list[str]] = {}
    # Two chapters with the same title in one TOC merge into one S3 key, so the
    # book only holds whichever the lambda wrote last. Neither row can be
    # ticked from it.
    shared = {
        key
        for key in (gate.get("key") for gate in checks.values())
        if key and sum(1 for gate in checks.values() if gate.get("key") == key) > 1
    }
    for chapter_id, gate in checks.items():
        row = rows.get(chapter_id)
        verdict = chapter_high_confidence(
            gate,
            row_rules=None if row is None else row["rules"],
            fonts_without_tounicode=int(gate.get("fontsWithoutToUnicode") or 0),
        )
        if gate.get("key") in shared:
            verdict = {
                "high": False,
                "reasons": verdict["reasons"]
                + [f"another chapter writes the same book, {gate['key']}"],
            }
        if verdict["high"]:
            notes[chapter_id] = (
                "auto run rebuilt the chapter book from the pushed previews and "
                f"the Adobe API passed every row rule, {reports}"
            )
        else:
            not_ticked[chapter_id] = verdict["reasons"]
    ticked = tick_tracker_chapter_rows(tracker, course_id, notes, date=date)
    for chapter_id in notes:
        if chapter_id not in ticked:
            not_ticked[chapter_id] = ["row already ticked"]
    return {"ticked": ticked, "notTicked": not_ticked}


def rebuild_and_check(
    args: argparse.Namespace,
    stem: Path,
    run_at: datetime,
    summary: dict[str, Any],
    summary_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rebuild the course on dev, check the chapter books and tick their rows.

    The push has already happened by the time this runs, so an AWS fault here
    must not cost the tracker sentence that records it: every failure is
    reported as `rebuild["error"]` instead of raised.
    """
    def checkpoint(stage: dict[str, Any]) -> None:
        summary["stages"]["rebuild"] = stage
        _write_json(summary_path, summary)

    try:
        return _rebuild_and_check(args, stem, run_at, summary, summary_path, checkpoint)
    except Exception as error:
        rebuild = dict(summary["stages"].get("rebuild") or {})
        rebuild["error"] = f"{type(error).__name__}: {error}"
        checkpoint(rebuild)
        return rebuild, {"ticked": [], "notTicked": {}}


def _rebuild_and_check(
    args: argparse.Namespace,
    stem: Path,
    run_at: datetime,
    summary: dict[str, Any],
    summary_path: Path,
    checkpoint: Callable[[dict[str, Any]], None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    rebuild = stage_rebuild(
        args,
        boto3.client("s3"),
        _lambda_client(),
        bucket=channels_bucket(args.env),
        checkpoint=checkpoint,
    )
    checkpoint(rebuild)

    rebuilt = dict((rebuild.get("wait") or {}).get("rebuilt") or {})
    if rebuilt and args.tracker and not args.check_all_worksheets:
        rows = tracker_chapter_rows(Path(args.tracker), args.course_id)
        rebuild["checkSkipped"] = {
            chapter_id: "row already ticked" if chapter_id in rows else "no tracker row"
            for chapter_id in rebuilt
            if chapter_id not in rows or rows[chapter_id]["done"]
        }
        rebuilt = {
            chapter_id: key
            for chapter_id, key in rebuilt.items()
            if chapter_id not in rebuild["checkSkipped"]
        }
    worksheet_dir = stem.with_name(f"{stem.name}-worksheet-checks")
    worksheet_checks = stage_worksheet_check(
        rebuilt,
        boto3.client("s3"),
        bucket=channels_bucket(args.env),
        workers=args.workers,
        out_dir=worksheet_dir,
    )
    summary["stages"]["worksheetCheck"] = {
        "checked": len(worksheet_checks),
        "passed": sum(1 for gate in worksheet_checks.values() if gate.get("pass")),
        "reports": str(worksheet_dir),
        "chapters": worksheet_checks,
    }
    _write_json(summary_path, summary)

    chapter_ticks: dict[str, Any] = {"ticked": [], "notTicked": {}}
    if args.tracker and worksheet_checks:
        chapter_ticks = stage_tick_chapter_rows(
            Path(args.tracker),
            args.course_id,
            worksheet_checks,
            date=run_at.strftime("%Y-%m-%d"),
            reports=str(worksheet_dir),
        )
        summary["stages"]["chapterTick"] = chapter_ticks
        _write_json(summary_path, summary)
    return rebuild, chapter_ticks


def _rebuild_sentence(rebuild: dict[str, Any], chapter_ticks: dict[str, Any]) -> str:
    """One clause for the tracker saying what the chapter rebuild did."""
    stale = "so the dev downloads and chapter books still predate this run."
    if rebuild.get("skipped"):
        return f"generateCoursePdf not invoked ({rebuild['skipped']}), {stale}"
    if rebuild.get("error"):
        return f"generateCoursePdf failed ({rebuild['error']}), {stale}"
    wait = rebuild.get("wait") or {}
    total = len(rebuild.get("expectedWorksheets") or {})
    done = len(wait.get("rebuilt") or {})
    ticked = len(chapter_ticks["ticked"])
    sentence = (
        f"generateCoursePdf rebuilt the topic downloads and {done} of {total} "
        f"chapter books, {ticked} chapter row{'' if ticked == 1 else 's'} ticked"
    )
    if wait.get("timedOut"):
        sentence += f", still waiting on {', '.join(sorted(wait.get('pending') or {}))}"
    sentence += "."
    warning = rebuild.get("dispatchWarning")
    if warning:
        sentence += f" {warning[0].upper()}{warning[1:]}."
    return sentence


def render_progress_page(tracker: Path, out: Path) -> dict[str, Any]:
    """Re-render the HTML view of the tracker; a failure is recorded, not raised."""
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(progress_page.render(tracker.read_text(encoding="utf-8"), tracker.name))
    except Exception as error:
        return {"path": str(out), "error": str(error)}
    return {"path": str(out), "rendered": True}


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if args.rebuild_only and args.plan_only:
        # The rebuild writes every wrap and book through the lambda, which is
        # the opposite of what --plan-only promises.
        raise ValueError("--rebuild-only cannot be combined with --plan-only")
    run_at = datetime.now(timezone.utc)
    stamp = run_at.strftime("%Y%m%dT%H%M%SZ")
    stem = Path(args.report_dir) / f"{args.course_id}-auto-{stamp}"
    summary: dict[str, Any] = {
        "schemaVersion": 1,
        "courseId": args.course_id,
        "environment": args.env,
        "runAt": run_at.isoformat(),
        "planOnly": args.plan_only,
        "policy": {
            "maxRenderDiff": args.max_render_diff,
            "adobeCheck": "zero failed rules on the repaired file",
            "autotag": not args.skip_autotag,
        },
        "stages": {},
    }
    summary_path = stem.with_suffix(".auto.json")

    if args.rebuild_only:
        summary["mode"] = "rebuildOnly"
        rebuild, chapter_ticks = rebuild_and_check(
            args, stem, run_at, summary, summary_path
        )
        if args.tracker:
            sentence = (
                f"Chapter rebuild {run_at.strftime('%Y-%m-%d')} "
                f"(`scripts/auto_fix_course.py --rebuild-only`, {summary_path}): "
                + _rebuild_sentence(rebuild, chapter_ticks)
            )
            summary["tracker"] = {
                "path": args.tracker,
                "sentence": sentence,
                "result": append_tracker_note(Path(args.tracker), args.course_id, sentence),
            }
            if args.progress_html:
                summary["tracker"]["html"] = render_progress_page(
                    Path(args.tracker), Path(args.progress_html)
                )
            _write_json(summary_path, summary)
        print(
            json.dumps(
                {
                    "summary": str(summary_path),
                    "rebuild": rebuild,
                    "chapterTick": chapter_ticks,
                },
                separators=(",", ":"),
            )
        )
        return 2 if rebuild.get("error") else 0

    topic_ids = None if args.all_topics else open_topic_ids(args.tracker, args.course_id)
    summary["topicSelection"] = (
        "allTopics" if topic_ids is None else {"openTrackerRows": topic_ids}
    )
    if topic_ids is not None and not topic_ids:
        summary["stages"]["sweep"] = {"skipped": "no open topic rows in the tracker"}
        _write_json(summary_path, summary)
        print(json.dumps({"summary": str(summary_path), "topicsSwept": 0}))
        return 0

    sweep_manifest_path, sweep_exit = run_sweep(args, stem, topic_ids)
    sweep_manifest = json.loads(sweep_manifest_path.read_text(encoding="utf-8"))
    topics: list[dict[str, Any]] = list(sweep_manifest.get("topics") or [])
    summary["stages"]["sweep"] = {
        "manifest": str(sweep_manifest_path),
        "exitCode": sweep_exit,
        "counts": sweep_manifest.get("counts"),
    }
    _write_json(summary_path, summary)

    diagnostics_dir = stem.with_name(f"{stem.name}-diagnostics")
    topics, autotag_log = stage_autotag(
        topics,
        workers=args.workers,
        skip=args.skip_autotag,
        diagnostics_dir=diagnostics_dir,
    )
    summary["stages"]["autotag"] = autotag_log
    _write_json(summary_path, summary)

    checks = stage_adobe_check(
        topics,
        workers=args.workers,
        include_unchanged=args.check_unchanged,
        out_dir=stem.with_name(f"{stem.name}-adobe-checks"),
    )
    summary["stages"]["adobeCheck"] = {
        "checked": len(checks),
        "passed": sum(1 for gate in checks.values() if gate.get("pass")),
    }
    _write_json(summary_path, summary)

    decisions: dict[str, dict[str, Any]] = {}
    for topic in topics:
        topic_id = str(topic.get("topicId"))
        decisions[topic_id] = decide_topic(
            topic,
            adobe=checks.get(topic_id),
            autotagged=bool(topic.get("autotagged")),
            max_render_diff=args.max_render_diff,
            max_render_diff_observed=topic.get("maxRenderDiff"),
        )
    sweep_manifest["topics"] = topics
    sweep_manifest["sourceManifest"] = str(sweep_manifest_path)
    publish_manifest_payload = build_publish_manifest(
        sweep_manifest, decisions, policy=summary["policy"]
    )
    publish_manifest_path = stem.with_suffix(".auto.manifest.json")
    _write_json(publish_manifest_path, publish_manifest_payload)
    review = [
        {
            "topicId": topic.get("topicId"),
            "topicTitle": topic.get("topicTitle"),
            "chapterId": topic.get("chapterId"),
            "resultKind": topic.get("resultKind"),
            "residualCategories": topic.get("residualCategories"),
            "reSweptPdf": topic.get("reSweptPdf"),
            "adobe": checks.get(str(topic.get("topicId"))),
            "reasons": decisions[str(topic.get("topicId"))]["reasons"],
        }
        for topic in topics
        if not decisions[str(topic.get("topicId"))]["publishable"]
    ]
    review_path = stem.with_suffix(".auto.review.json")
    _write_json(review_path, {"courseId": args.course_id, "topics": review})
    held_unchanged = sum(
        1 for item in review if "unchanged by the sweep" in item["reasons"]
    )
    summary["stages"]["decision"] = {
        "publishable": len(publish_manifest_payload["topics"]),
        "held": len(review) - held_unchanged,
        "unchanged": held_unchanged,
        "manifest": str(publish_manifest_path),
        "reviewQueue": str(review_path),
    }
    _write_json(summary_path, summary)

    publish_outcome: dict[str, Any] = {"skipped": True}
    publish_error: str | None = None
    if publish_manifest_payload["topics"]:
        render_exceptions = {
            topic_id
            for topic_id, decision in decisions.items()
            if decision["publishable"] and decision["renderException"]
        }
        residual_exceptions = {
            topic_id
            for topic_id, decision in decisions.items()
            if decision["publishable"] and decision.get("residualException")
        }
        report_path = (
            stem.with_suffix(".replacement-plan.json")
            if args.plan_only
            else Path(args.report_dir) / f"{args.course_id}-s3-replacement-audit-{stamp}.json"
        )
        distribution_id = cloudfront_distribution_id(args.env)

        def invalidate(paths: list[str]) -> str | None:
            return invalidate_paths(distribution_id, paths)

        try:
            result = publish_manifest(
                publish_manifest_path,
                report_path,
                ROOT,
                boto3.client("s3"),
                apply=not args.plan_only,
                approved_manifest_sha256=manifest_sha256(publish_manifest_path),
                approved_exceptions=residual_exceptions,
                approved_render_exceptions=render_exceptions,
                invalidate=None if args.plan_only else invalidate,
            )
            publish_outcome = {
                "report": str(report_path),
                "outcome": result["outcome"],
                "summary": result["summary"],
                "renderExceptions": sorted(render_exceptions),
                "residualExceptions": sorted(residual_exceptions),
            }
        except Exception as error:
            publish_error = str(error)
            publish_outcome = {"report": str(report_path), "error": publish_error}
    summary["stages"]["publish"] = publish_outcome
    _write_json(summary_path, summary)

    ticks: dict[str, Any] = {"ticked": [], "notTicked": {}}
    if args.tracker and not args.plan_only and not publish_error:
        ticks = stage_tick_rows(
            Path(args.tracker),
            args.course_id,
            topics,
            checks,
            json.loads(Path(publish_outcome["report"]).read_text(encoding="utf-8"))
            if publish_outcome.get("report")
            else {},
            date=run_at.strftime("%Y-%m-%d"),
            audit=str(publish_outcome.get("report")),
        )
        summary["stages"]["tick"] = ticks
        _write_json(summary_path, summary)

    pushed_count = int((publish_outcome.get("summary") or {}).get("verified") or 0)
    skip_rebuild = (
        "--skip-rebuild"
        if args.skip_rebuild
        else "plan only"
        if args.plan_only
        else "publish failed"
        if publish_error
        else "nothing was pushed"
        if not pushed_count
        else "generateCoursePdf answers 403 outside the dev stage"
        if args.env != "dev"
        else None
    )
    rebuild: dict[str, Any] = {"skipped": skip_rebuild}
    chapter_ticks: dict[str, Any] = {"ticked": [], "notTicked": {}}
    if skip_rebuild is None:
        rebuild, chapter_ticks = rebuild_and_check(
            args, stem, run_at, summary, summary_path
        )

    if args.tracker:
        decision = summary["stages"]["decision"]
        pushed = (
            "publish failed: " + publish_error
            if publish_error
            else "plan only, nothing pushed"
            if args.plan_only
            else f"{publish_outcome.get('summary', {}).get('verified', 0)} pushed to dev, "
            f"{len(ticks['ticked'])} high-confidence rows ticked"
        )
        sentence = (
            f"Auto run {run_at.strftime('%Y-%m-%d')} (`scripts/auto_fix_course.py`): "
            f"{len(topics)} topics swept, "
            f"{sum(1 for item in autotag_log.values() if 'resultKind' in item)} Auto-Tagged, "
            f"{summary['stages']['adobeCheck']['passed']} of "
            f"{summary['stages']['adobeCheck']['checked']} passed the Adobe API check, "
            f"{decision['publishable']} publishable, {pushed}; "
            f"{decision['held']} held for review in {review_path}; "
            f"{decision['unchanged']} unchanged by the sweep. "
            f"{_rebuild_sentence(rebuild, chapter_ticks)} "
            "Other rows stay unticked until the user checks dev."
        )
        summary["tracker"] = {
            "path": args.tracker,
            "result": append_tracker_note(Path(args.tracker), args.course_id, sentence),
        }
        if args.progress_html:
            summary["tracker"]["html"] = render_progress_page(
                Path(args.tracker), Path(args.progress_html)
            )
        _write_json(summary_path, summary)

    print(
        json.dumps(
            {
                "summary": str(summary_path),
                "manifest": str(publish_manifest_path),
                "reviewQueue": str(review_path),
                "decision": summary["stages"]["decision"],
                "publish": publish_outcome,
                "rebuild": summary["stages"].get("rebuild", rebuild),
                "chapterTick": chapter_ticks,
            },
            separators=(",", ":"),
        )
    )
    return 2 if publish_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
