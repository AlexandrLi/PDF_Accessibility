#!/usr/bin/env python3
"""Download and locally re-sweep PDFs selected by an accessibility issue map.

This runner deliberately does not invoke the Adobe/Step Functions remediation
path or write to S3. It reuses the repository's generic local sweep modules,
preserving downloaded originals and recording every per-sweep failure.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import unicodedata
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import boto3
import pikepdf

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from lib.bookmark_sweep import repair_bookmarks  # noqa: E402
from lib.character_encoding_sweep import repair_character_encoding  # noqa: E402
from lib.config import channels_bucket  # noqa: E402
from lib.figure_alt_sweep import repair_missing_figure_alt  # noqa: E402
from lib.heading_nesting_sweep import repair_heading_nesting  # noqa: E402
from lib.inline_formula_sweep import repair_inline_formula_figures  # noqa: E402
from lib.layout_table_sweep import repair_layout_tables  # noqa: E402
from lib.marked_content_actualtext_sweep import (  # noqa: E402
    repair_marked_content_actualtext,
)
from lib.pdf_a11y_audit import audit_pdf_bytes  # noqa: E402
from lib.tab_order_sweep import repair_tab_order  # noqa: E402
from lib.tagged_annotation_sweep import repair_tagged_annotations  # noqa: E402
from lib.tagged_content_sweep import repair_tagged_content  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--map",
        default="reports/accessibility-topic-fix-map.json",
        help="JSON or CSV issue map generated from the validation workbook",
    )
    parser.add_argument("--course-id", required=True)
    parser.add_argument(
        "--map-sheet",
        default=None,
        help="Exact issue-map sheet name when it differs from course metadata title",
    )
    parser.add_argument("--env", choices=("dev", "prod"), default="dev")
    parser.add_argument(
        "--output-root",
        default="pdfs/accessibility-issue-map",
        help="Parent directory for course/date grouped original and re-swept PDFs",
    )
    parser.add_argument("--original-dir", help="Override the grouped originals directory")
    parser.add_argument("--swept-dir", help="Override the grouped re-swept directory")
    parser.add_argument("--report", help="Override the generated JSON report path")
    return parser.parse_args()


def normalized(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def normalized_course_sheet(value: object) -> str:
    return normalized(value).replace("introduction ", "intro ", 1)


def normalized_chapter_title(value: object) -> str:
    text = normalized(value)
    text = re.sub(r"^\d+\.\s*", "", text)
    text = re.sub(r"^review\s+\d+\s*[:\-]?\s*", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def load_issue_rows(
    path: Path,
    course_title: str,
    map_sheet: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, int | None]:
    source_workbook: str | None = None
    map_topic_rows: int | None = None
    if path.suffix.casefold() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        source_workbook = payload.get("source")
        map_topic_rows = (payload.get("summary") or {}).get("topicRows")
        raw_rows = payload.get("topics") or []
        rows = [
            {
                "sheet": row.get("sheet"),
                "row": str(row.get("row") or ""),
                "topic_title": row.get("topicTitle") or "",
                "chapter_id": (row.get("chapterContext") or {}).get("id") or "",
                "chapter_title": (row.get("chapterContext") or {}).get("title") or "",
                "chapter_row": str((row.get("chapterContext") or {}).get("row") or ""),
                "failed_columns": ", ".join(row.get("failedColumns") or []),
                "failed_categories": "; ".join(row.get("failedCategories") or []),
                "failure_count": str(row.get("failureCount") or ""),
                "topic_id": row.get("topicId") or "",
                "pdf_key": row.get("pdfKey") or "",
            }
            for row in raw_rows
        ]
    else:
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))

    if map_sheet:
        selected_sheet = normalized_course_sheet(map_sheet)
        candidate_sheets = sorted(
            {
                normalized_course_sheet(row.get("sheet"))
                for row in rows
                if normalized_course_sheet(row.get("sheet")) == selected_sheet
            }
        )
    else:
        course_prefix = normalized_course_sheet(course_title)
        candidate_sheets = sorted(
            {
                normalized_course_sheet(row.get("sheet"))
                for row in rows
                if normalized_course_sheet(row.get("sheet")) == course_prefix
                or normalized_course_sheet(row.get("sheet")).startswith(
                    f"{course_prefix} -"
                )
            }
        )
    if len(candidate_sheets) != 1:
        raise ValueError(
            f"Expected one issue-map sheet for course {course_title!r}; "
            f"found {candidate_sheets or 'none'}"
        )
    selected_sheet = candidate_sheets[0]
    return (
        [
            row
            for row in rows
            if normalized_course_sheet(row.get("sheet")) == selected_sheet
        ],
        source_workbook,
        map_topic_rows,
    )


def resolve_topics(
    issue_rows: list[dict[str, Any]],
    course: dict[str, Any],
    course_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    details = course.get("details") or {}
    toc_id = details.get("defaultToc")
    tocs = course.get("tocs") or {}
    toc = tocs.get(toc_id) if toc_id else None
    if not toc_id or not toc:
        raise ValueError("Course metadata does not provide a valid default TOC")

    topics = course.get("topics") or {}
    toc_topic_chapters: dict[str, list[dict[str, Any]]] = {}
    for chapter in toc.get("chapters") or []:
        for reference in chapter.get("topics") or []:
            topic_id = reference.get("id")
            if topic_id:
                toc_topic_chapters.setdefault(topic_id, []).append(chapter)

    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for row in issue_rows:
        title = row.get("topic_title") or ""
        title_matches = [
            topic_id
            for topic_id, topic in topics.items()
            if normalized(topic.get("title")) == normalized(title)
        ]
        candidates = [
            topic_id for topic_id in title_matches if topic_id in toc_topic_chapters
        ]
        context_title = row.get("chapter_title") or ""
        context_matches = [
            topic_id
            for topic_id in candidates
            if any(
                normalized_chapter_title(chapter.get("title"))
                == normalized_chapter_title(context_title)
                for chapter in toc_topic_chapters[topic_id]
            )
        ]
        if len(context_matches) != 1:
            options = []
            for topic_id in candidates:
                options.append(
                    {
                        "topicId": topic_id,
                        "chapters": [
                            {
                                "id": chapter.get("id"),
                                "title": chapter.get("title"),
                            }
                            for chapter in toc_topic_chapters[topic_id]
                        ],
                    }
                )
            unmatched.append(
                {
                    "sourceIssueRow": row,
                    "reason": (
                        "no reliable metadata match"
                        if not context_matches
                        else "ambiguous metadata match"
                    ),
                    "candidateOptions": options,
                }
            )
            continue

        topic_id = context_matches[0]
        topic = topics[topic_id]
        if not topic.get("pdfAvailable"):
            unmatched.append(
                {
                    "sourceIssueRow": row,
                    "reason": "matched topic has pdfAvailable=false",
                    "topicId": topic_id,
                }
            )
            continue
        chapter = next(
            chapter
            for chapter in toc_topic_chapters[topic_id]
            if normalized_chapter_title(chapter.get("title"))
            == normalized_chapter_title(context_title)
        )
        matched.append(
            {
                "sourceIssueRow": row,
                "topicId": topic_id,
                "topicTitle": topic.get("title") or title,
                "chapterId": chapter.get("id"),
                "chapterTitle": chapter.get("title"),
                "pdfKey": f"courses/{course_id}/topic_pdfs/{topic_id}.pdf",
            }
        )
    return matched, unmatched, toc_id


def serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    return str(value)


def validate_pdf(pdf_bytes: bytes) -> dict[str, Any]:
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        return {"open": True, "pages": len(pdf.pages)}


def run_sweeps(pdf_bytes: bytes) -> tuple[bytes, dict[str, Any]]:
    current = pdf_bytes
    repairs: dict[str, Any] = {}
    warnings: list[str] = []
    applied: list[str] = []
    audit_before: dict[str, Any] | None = None

    def run_stage(
        name: str,
        repair: Callable[[bytes], tuple[bytes, Any]],
    ) -> Any | None:
        nonlocal current
        before = current
        try:
            current, result = repair(current)
            repairs[name] = serialize(result)
            if current != before:
                applied.append(name)
            return result
        except Exception as error:
            current = before
            repairs[name] = None
            warnings.append(f"{name} failed: {error}")
            return None

    run_stage("tabOrder", repair_tab_order)
    run_stage("taggedContent", repair_tagged_content)
    run_stage("taggedAnnotations", repair_tagged_annotations)

    try:
        audit_before = audit_pdf_bytes(current).to_dict()
        repairs["auditBeforeRepairs"] = audit_before
    except Exception as error:
        warnings.append(f"audit before repairs failed: {error}")
        repairs["auditBeforeRepairs"] = None

    if audit_before and audit_before.get("figures_missing_alt"):
        run_stage("figureAlt", repair_missing_figure_alt)
    else:
        repairs["figureAlt"] = {"skipped": "no missing figure alt reported"}

    run_stage("inlineFormula", repair_inline_formula_figures)
    run_stage("layoutTable", repair_layout_tables)
    run_stage("markedContentActualText", repair_marked_content_actualtext)
    run_stage("characterEncoding", repair_character_encoding)
    run_stage("headingNesting", repair_heading_nesting)
    run_stage("bookmarks", repair_bookmarks)

    try:
        final_audit = audit_pdf_bytes(current)
        repairs["auditAfterRepairs"] = final_audit.to_dict()
    except Exception as error:
        repairs["auditAfterRepairs"] = None
        warnings.append(f"audit after repairs failed: {error}")

    return current, {
        "appliedRepairs": applied,
        "repairs": repairs,
        "warnings": warnings,
    }


def main() -> int:
    args = parse_args()
    run_at = datetime.now(timezone.utc)
    run_date = run_at.strftime("%Y%m%d")
    run_dir = Path(args.output_root) / args.course_id / run_date
    map_path = Path(args.map)
    original_dir = Path(args.original_dir) if args.original_dir else run_dir / "originals"
    swept_dir = Path(args.swept_dir) if args.swept_dir else run_dir / "reswept"
    report_path = (
        Path(args.report)
        if args.report
        else Path("reports") / f"{args.course_id}-issue-map-sweep-{run_date}.json"
    )
    original_dir.mkdir(parents=True, exist_ok=True)
    swept_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    s3 = boto3.client("s3")
    bucket = channels_bucket(args.env)
    metadata_key = f"courses/{args.course_id}/{args.course_id}.json"
    course = json.loads(
        s3.get_object(Bucket=bucket, Key=metadata_key)["Body"].read().decode("utf-8")
    )
    course_title = (course.get("details") or {}).get("title") or args.course_id
    issue_rows, source_workbook, map_topic_rows = load_issue_rows(
        map_path,
        course_title,
        args.map_sheet,
    )
    matched, unmatched, toc_id = resolve_topics(issue_rows, course, args.course_id)
    results: list[dict[str, Any]] = []
    counts = {
        "requested": len(issue_rows),
        "matched": len(matched),
        "downloaded": 0,
        "swept": 0,
        "failed": 0,
        "unmatched": len(unmatched),
    }

    for item in matched:
        topic_id = item["topicId"]
        original_path = original_dir / f"{topic_id}.pdf"
        swept_path = swept_dir / f"{topic_id}.pdf"
        entry = {**item, "originalPdf": str(original_path), "reSweptPdf": str(swept_path)}
        try:
            pdf_bytes = s3.get_object(Bucket=bucket, Key=item["pdfKey"])["Body"].read()
            original_path.write_bytes(pdf_bytes)
            counts["downloaded"] += 1
            entry["originalValidation"] = validate_pdf(pdf_bytes)
            swept_bytes, sweep_result = run_sweeps(pdf_bytes)
            swept_path.write_bytes(swept_bytes)
            entry.update(sweep_result)
            entry["reSweptValidation"] = validate_pdf(swept_bytes)
            try:
                second_pass_bytes, second_pass_result = run_sweeps(swept_bytes)
                entry["secondPass"] = {
                    "byteStable": second_pass_bytes == swept_bytes,
                    "appliedRepairs": second_pass_result["appliedRepairs"],
                    "warnings": second_pass_result["warnings"],
                }
                if second_pass_bytes != swept_bytes:
                    entry["warnings"].append(
                        "second pass was not byte-stable; first-pass output preserved"
                    )
            except Exception as error:
                entry["secondPass"] = {"error": str(error)}
                entry["warnings"].append(f"second pass validation failed: {error}")
            counts["swept"] += 1
            entry["status"] = (
                "swept-with-warnings" if entry["warnings"] else "swept"
            )
        except Exception as error:
            counts["failed"] += 1
            entry["status"] = "failed"
            entry["errors"] = [str(error)]
        results.append(entry)

    report = {
        "runAt": run_at.isoformat(),
        "sourceIssueMap": str(map_path.resolve()),
        "sourceWorkbook": source_workbook,
        "course": {
            "selection": course_title,
            "courseId": args.course_id,
            "courseTitle": (course.get("details") or {}).get("title"),
            "environment": args.env,
            "bucket": bucket,
            "defaultTocId": toc_id,
        },
        "scope": {
            "topicRowsOnly": True,
            "issueMapTopicRows": map_topic_rows,
            "selectedCourseSheetRows": len(issue_rows),
            "chapterAggregateRowsDownloaded": 0,
            "pdfPattern": "courses/{courseId}/topic_pdfs/{topicId}.pdf",
        },
        "counts": counts,
        "results": results,
        "unmatched": unmatched,
        "notes": [
            "Original PDFs are preserved separately from re-swept PDFs.",
            "This is a local generic sweep run; no Adobe checker was run.",
            "Adobe/PDF accessibility conformance remains subject to manual Adobe checking.",
        ],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"report": str(report_path), "counts": counts}, indent=2))
    return 1 if counts["failed"] or counts["unmatched"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
