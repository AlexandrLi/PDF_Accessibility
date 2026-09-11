#!/usr/bin/env python3
"""Download and locally re-sweep PDFs selected by an accessibility issue map.

This runner deliberately does not invoke the Adobe/Step Functions remediation
path or write to S3. It reuses the repository's generic local sweep modules,
preserving downloaded originals and recording every per-sweep failure.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from lib.accessibility_course_workflow import (  # noqa: E402
    pipeline_fingerprint,
    prepare_pdf,
    run_sweeps,
    serialize,
    sha256_bytes,
    validate_pdf,
)
from lib.channels_paths import load_course_json, preview_key  # noqa: E402
from lib.config import channels_bucket  # noqa: E402


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
    parser.add_argument(
        "--all-topics",
        action="store_true",
        help=(
            "Sweep every topic in the course default TOC with pdfAvailable=true "
            "instead of only the topics listed in the issue map; the map file "
            "is not read"
        ),
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
    parser.add_argument("--manifest", help="Override the compact manifest path")
    parser.add_argument("--diagnostics-dir", help="Override per-topic diagnostics directory")
    parser.add_argument("--review-queue", help="Override the compact review queue path")
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Maximum parallel downloads and PDF preparations",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse verified outputs when source and pipeline fingerprints match",
    )
    parser.add_argument(
        "--title-alias",
        action="append",
        default=[],
        metavar="WORKBOOK TITLE==COURSE TITLE",
        help=(
            "Map a workbook topic title to the course-metadata title when the "
            "workbook drifted (repeatable); matched case-insensitively"
        ),
    )
    return parser.parse_args()


# Workbooks and course metadata disagree on typographic vs ASCII punctuation
# (e.g. DeMoivre's vs DeMoivre's); NFKC does not fold these.
_TITLE_PUNCTUATION_FOLD = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "ʼ": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
    }
)


def normalized(value: object) -> str:
    text = (
        unicodedata.normalize("NFKC", str(value or ""))
        .translate(_TITLE_PUNCTUATION_FOLD)
        .casefold()
    )
    return " ".join(text.split())


def normalized_course_sheet(value: object) -> str:
    return normalized(value).replace("introduction ", "intro ", 1)


def chapter_number(value: object) -> str:
    text = str(value or "")
    match = re.match(r"^\s*0*(\d+)\s*\.", text)
    if not match:
        match = re.match(r"^\s*ch(?:apter)?\.?\s*0*(\d+)\b", text, re.IGNORECASE)
    return match.group(1) if match else ""


def normalized_chapter_title(value: object) -> str:
    text = normalized(value)
    text = re.sub(r"^ch(?:apter)?\.?\s*\d+\s*[:\-.]?\s*", "", text)
    text = re.sub(r"^\d+\.\s*", "", text)
    text = re.sub(r"^review\s+\d+\s*[:\-]?\s*", "", text)
    text = text.replace("&", "and")
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


def parse_title_aliases(raw_aliases: list[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for raw in raw_aliases:
        workbook_title, separator, course_title = raw.partition("==")
        if not separator or not workbook_title.strip() or not course_title.strip():
            raise ValueError(
                f"--title-alias must look like 'WORKBOOK TITLE==COURSE TITLE': {raw!r}"
            )
        aliases[normalized(workbook_title)] = normalized(course_title)
    return aliases


def resolve_topics(
    issue_rows: list[dict[str, Any]],
    course: dict[str, Any],
    course_id: str,
    title_aliases: dict[str, str] | None = None,
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
    topics_by_title: dict[str, list[str]] = {}
    for topic_id, topic in topics.items():
        topics_by_title.setdefault(normalized(topic.get("title")), []).append(topic_id)
    chapter_titles_by_topic = {
        topic_id: {
            normalized_chapter_title(chapter.get("title"))
            for chapter in chapters
        }
        for topic_id, chapters in toc_topic_chapters.items()
    }

    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for row in issue_rows:
        title = row.get("topic_title") or ""
        title_lookup = normalized(title)
        if title_aliases:
            title_lookup = title_aliases.get(title_lookup, title_lookup)
        title_matches = topics_by_title.get(title_lookup, [])
        candidates = [
            topic_id for topic_id in title_matches if topic_id in toc_topic_chapters
        ]
        context_title = row.get("chapter_title") or ""
        normalized_context = normalized_chapter_title(context_title)
        context_matches = [
            topic_id
            for topic_id in candidates
            if normalized_context in chapter_titles_by_topic[topic_id]
        ]
        chosen: tuple[str, dict[str, Any], str] | None = None
        if len(context_matches) == 1:
            topic_id = context_matches[0]
            chapter = next(
                chapter
                for chapter in toc_topic_chapters[topic_id]
                if normalized_chapter_title(chapter.get("title")) == normalized_context
            )
            chosen = (topic_id, chapter, "chapterTitle")
        elif len(candidates) == 1:
            # The topic title is unique in the TOC but the workbook chapter
            # title drifted from course metadata. Accept the sole candidate
            # only when the workbook chapter number corroborates its chapter.
            row_chapter_number = str(row.get("chapter_id") or "").strip().lstrip("0")
            numbered_chapters = [
                chapter
                for chapter in toc_topic_chapters[candidates[0]]
                if row_chapter_number
                and chapter_number(chapter.get("title")) == row_chapter_number
            ]
            if len(numbered_chapters) == 1:
                chosen = (candidates[0], numbered_chapters[0], "uniqueTitleChapterNumber")
        if chosen is None:
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

        topic_id, chapter, match_strategy = chosen
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
        matched.append(
            {
                "sourceIssueRow": row,
                "topicId": topic_id,
                "topicTitle": topic.get("title") or title,
                "chapterId": chapter.get("id"),
                "chapterTitle": chapter.get("title"),
                "matchStrategy": match_strategy,
                "pdfKey": preview_key(course_id, topic_id),
            }
        )
    return matched, unmatched, toc_id


def resolve_all_topics(
    course: dict[str, Any],
    course_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Build a matched-topic list for every default-TOC topic with a preview PDF.

    Mirrors the entry shape of resolve_topics so the rest of the sweep, the
    manifest and the publish preflight see no difference. The synthetic
    sourceIssueRow carries no failed categories, so residual diagnostics fall
    back to the post-sweep audit alone. Topics without a preview PDF are
    returned in the second list as skipped, not unmatched: they are not
    failures of the sweep.
    """
    details = course.get("details") or {}
    toc_id = details.get("defaultToc")
    toc = (course.get("tocs") or {}).get(toc_id) if toc_id else None
    if not toc_id or not toc:
        raise ValueError("Course metadata does not provide a valid default TOC")

    topics = course.get("topics") or {}
    matched: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chapter in toc.get("chapters") or []:
        for reference in chapter.get("topics") or []:
            topic_id = reference.get("id")
            if not topic_id or topic_id in seen:
                continue
            seen.add(topic_id)
            topic = topics.get(topic_id) or {}
            row = {
                "sheet": None,
                "row": "",
                "topic_title": topic.get("title") or "",
                "chapter_id": chapter.get("id") or "",
                "chapter_title": chapter.get("title") or "",
                "chapter_row": "",
                "failed_columns": "",
                "failed_categories": "",
                "failure_count": "",
                "topic_id": topic_id,
                "pdf_key": preview_key(course_id, topic_id),
            }
            if not topic.get("pdfAvailable"):
                skipped.append(
                    {
                        "topicId": topic_id,
                        "topicTitle": topic.get("title") or topic_id,
                        "chapterId": chapter.get("id"),
                        "reason": "pdfAvailable=false",
                    }
                )
                continue
            matched.append(
                {
                    "sourceIssueRow": row,
                    "topicId": topic_id,
                    "topicTitle": topic.get("title") or topic_id,
                    "chapterId": chapter.get("id"),
                    "chapterTitle": chapter.get("title"),
                    "matchStrategy": "allTopics",
                    "pdfKey": preview_key(course_id, topic_id),
                }
            )
    return matched, skipped, toc_id


def _category_keys(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = re.split(r"[;,]", str(value or ""))
    return {
        re.sub(r"[^a-z0-9]", "", normalized(item))
        for item in values
        if normalized(item)
    }


def _audit_category_diagnostics(
    audit: dict[str, Any] | None,
    category: str,
) -> dict[str, Any]:
    if not audit:
        return {"status": "unverifiable", "reason": "post-sweep audit unavailable"}
    if not audit.get("table_count"):
        return {
            "status": "unverifiable",
            "reason": "post-sweep structure contains no /Table",
        }
    if category == "tablesheaders":
        lacking_headers = audit.get("tables_without_th", 0)
        explicit_headers = bool(audit.get("data_cells_with_explicit_headers"))
        id_residuals = (
            (audit.get("tables_th_missing_id") or [])
            + (audit.get("tables_th_duplicate_id") or [])
            if explicit_headers
            else []
        )
        header_residuals = {
            "tablesWithoutTh": lacking_headers,
            "thMissingScope": audit.get("tables_th_missing_scope") or [],
            "thInvalidScope": audit.get("tables_th_invalid_scope") or [],
            "thMissingId": audit.get("tables_th_missing_id") or [],
            "thDuplicateId": audit.get("tables_th_duplicate_id") or [],
            "dataCellsMissingHeaders": audit.get("data_cells_missing_headers") or [],
            "dataCellsMalformedHeaders": audit.get("data_cells_malformed_headers")
            or [],
            "dataCellsUnresolvedHeaders": audit.get(
                "data_cells_unresolved_headers"
            )
            or [],
        }
        residual_values = {
            **header_residuals,
            "idsRequiredButInvalid": id_residuals,
        }
        return {
            "status": (
                "residual"
                if any(
                    value
                    for key, value in residual_values.items()
                    if key
                    not in {"dataCellsMissingHeaders", "thMissingId", "thDuplicateId"}
                )
                or bool(id_residuals)
                else "resolved"
            ),
            **header_residuals,
            "idsRequiredForAssociations": explicit_headers,
            "criterion": (
                "no /Table lacks /TH; usable TH scope; explicit /Headers resolve; "
                "TH IDs are required only for explicit associations"
            ),
        }
    if category == "tablesregularity":
        invalid_roles = audit.get("invalid_row_child_roles") or []
        inconsistent_widths = audit.get("tables_with_inconsistent_row_widths") or []
        residual = bool(invalid_roles or inconsistent_widths)
        return {
            "status": "residual" if residual else "resolved",
            "invalidRowChildRoles": invalid_roles,
            "tablesWithInconsistentRowWidths": inconsistent_widths,
            "logicalRowWidths": audit.get("logical_row_widths") or {},
            "criterion": "derivable rows have valid direct roles and consistent widths",
        }
    return {"status": "untracked"}


def build_residual_diagnostics(
    sweep_result: dict[str, Any],
    failed_categories: object,
    all_categories: bool = False,
) -> dict[str, Any]:
    """Return nonblocking, per-PDF residual telemetry for issue-map sweeps.

    With an issue map, the table checks run only for the categories the
    workbook flagged, and a PDF with no table is unverifiable because the
    workbook claimed one failed. With all_categories (the --all-topics sweep)
    there is no workbook claim, so the table checks run on every PDF that has
    a table and are simply not applicable to one that has none.
    """
    repairs = sweep_result.get("repairs") or {}
    layout = repairs.get("layoutTable")
    layout = layout if isinstance(layout, dict) else {}
    audit = repairs.get("auditAfterRepairs")
    audit = audit if isinstance(audit, dict) else None
    categories = _category_keys(failed_categories)
    tracked_categories: dict[str, dict[str, Any]] = {}
    if all_categories:
        if not audit:
            categories |= {"tablesheaders", "tablesregularity"}
        elif audit.get("table_count"):
            categories |= {"tablesheaders", "tablesregularity"}
    if "tablesheaders" in categories:
        tracked_categories["Tables Headers"] = _audit_category_diagnostics(
            audit, "tablesheaders"
        )
    if "tablesregularity" in categories:
        tracked_categories["Tables Regularity"] = _audit_category_diagnostics(
            audit, "tablesregularity"
        )
    if audit and audit.get("struct_tree_root_present") is False:
        tracked_categories["Tagged Content"] = {
            "status": "residual",
            "evidence": [
                "PDF has no structure tree after local sweeps; Adobe auto-tagging is required"
            ],
        }
    untagged_images = (audit or {}).get("nonfigure_image_mcids_missing_alt") or []
    if untagged_images:
        tracked_categories["Other Elements Alternate Text"] = {
            "status": "residual",
            "evidence": [
                f"image content without alternate text at {label}"
                for label in untagged_images
            ],
        }
    return {
        "layoutTable": {
            "unresolved": layout.get("unresolved") or [],
            "conflicts": layout.get("conflicts") or [],
        },
        "categories": tracked_categories,
    }


def classify_residual_status(
    diagnostics: dict[str, Any],
    warnings: list[str] | None = None,
) -> tuple[str, str]:
    """Classify execution separately from whether residuals remain."""
    layout = diagnostics.get("layoutTable") or {}
    layout_residual = bool(layout.get("unresolved") or layout.get("conflicts"))
    category_values = (diagnostics.get("categories") or {}).values()
    has_residual = layout_residual or any(
        value.get("status") == "residual"
        for value in category_values
        if isinstance(value, dict)
    )
    has_unverifiable = any(
        value.get("status") == "unverifiable"
        for value in category_values
        if isinstance(value, dict)
    )
    if has_residual:
        return "residual", "swept-with-residuals"
    if has_unverifiable:
        return "unverifiable", "swept-unverifiable"
    return "resolved", "swept-with-warnings" if warnings else "swept"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _compact_topic(
    entry: dict[str, Any],
    *,
    source_etag: str,
    diagnostics_path: Path,
) -> dict[str, Any]:
    original = entry.get("originalValidation") or {}
    swept = entry.get("reSweptValidation") or {}
    render = entry.get("renderValidation") or {}
    second = entry.get("secondPass") or {}
    categories = {
        name: value.get("status")
        for name, value in (
            (entry.get("residualDiagnostics") or {}).get("categories") or {}
        ).items()
        if isinstance(value, dict)
    }
    return {
        "topicId": entry.get("topicId"),
        "topicTitle": entry.get("topicTitle"),
        "chapterId": entry.get("chapterId"),
        "pdfKey": entry.get("pdfKey"),
        "originalPdf": entry.get("originalPdf"),
        "reSweptPdf": entry.get("reSweptPdf"),
        "sourceETag": source_etag,
        "originalSha256": original.get("sha256"),
        "reSweptSha256": swept.get("sha256"),
        "originalBytes": original.get("bytes"),
        "reSweptBytes": swept.get("bytes"),
        "pages": swept.get("pages"),
        "status": entry.get("status"),
        "resultKind": entry.get("resultKind"),
        "appliedRepairs": entry.get("appliedRepairs") or [],
        "warnings": entry.get("warnings") or [],
        "residualCategories": categories,
        "renderIdentical": render.get("identical"),
        "secondPassByteStable": second.get("byteStable"),
        "secondPassExecuted": second.get("executed"),
        "diagnostics": str(diagnostics_path),
    }


def _resume_topic(
    topic: dict[str, Any] | None,
    *,
    pipeline: str,
    manifest_pipeline: str | None,
    source_etag: str,
) -> bool:
    if not topic or manifest_pipeline != pipeline:
        return False
    if topic.get("sourceETag") != source_etag:
        return False
    if topic.get("status") not in {
        "swept",
        "swept-with-warnings",
        "swept-with-residuals",
        "swept-unverifiable",
    }:
        return False
    original_path = Path(str(topic.get("originalPdf") or ""))
    swept_path = Path(str(topic.get("reSweptPdf") or ""))
    if not original_path.is_file() or not swept_path.is_file():
        return False
    return (
        sha256_bytes(original_path.read_bytes()) == topic.get("originalSha256")
        and sha256_bytes(swept_path.read_bytes()) == topic.get("reSweptSha256")
        and topic.get("renderIdentical") is True
        and topic.get("secondPassByteStable") is True
    )


def _download_topic(
    s3: Any,
    bucket: str,
    item: dict[str, Any],
) -> tuple[dict[str, Any], str, bytes]:
    head = s3.head_object(Bucket=bucket, Key=item["pdfKey"])
    etag = str(head.get("ETag") or "").strip('"')
    body = s3.get_object(Bucket=bucket, Key=item["pdfKey"])["Body"].read()
    return item, etag, body


def _prepare_downloads(
    downloads: dict[str, tuple[dict[str, Any], str, bytes]],
    workers: int,
) -> tuple[dict[str, tuple[bytes, dict[str, Any]] | Exception], str]:
    if not downloads:
        return {}, "resume-only"
    if workers == 1:
        results: dict[str, tuple[bytes, dict[str, Any]] | Exception] = {}
        for topic_id, (_item, _etag, pdf_bytes) in downloads.items():
            try:
                results[topic_id] = prepare_pdf(pdf_bytes)
            except Exception as error:
                results[topic_id] = error
        return results, "serial"

    try:
        process_results: dict[str, tuple[bytes, dict[str, Any]] | Exception] = {}
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(prepare_pdf, pdf_bytes): topic_id
                for topic_id, (_item, _etag, pdf_bytes) in downloads.items()
            }
            for future in as_completed(futures):
                topic_id = futures[future]
                try:
                    process_results[topic_id] = future.result()
                except Exception as error:
                    process_results[topic_id] = error
        return process_results, "process"
    except (BrokenProcessPool, OSError, PermissionError):
        thread_results: dict[str, tuple[bytes, dict[str, Any]] | Exception] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(prepare_pdf, pdf_bytes): topic_id
                for topic_id, (_item, _etag, pdf_bytes) in downloads.items()
            }
            for future in as_completed(futures):
                topic_id = futures[future]
                try:
                    thread_results[topic_id] = future.result()
                except Exception as error:
                    thread_results[topic_id] = error
        return thread_results, "thread-fallback"


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
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
    manifest_path = (
        Path(args.manifest)
        if args.manifest
        else report_path.with_name(f"{report_path.stem}.manifest.json")
    )
    diagnostics_dir = (
        Path(args.diagnostics_dir)
        if args.diagnostics_dir
        else report_path.with_name(f"{report_path.stem}-diagnostics")
    )
    review_path = (
        Path(args.review_queue)
        if args.review_queue
        else report_path.with_name(f"{report_path.stem}.review.json")
    )
    original_dir.mkdir(parents=True, exist_ok=True)
    swept_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostics_dir.mkdir(parents=True, exist_ok=True)

    s3 = boto3.client("s3")
    bucket = channels_bucket(args.env)
    course = load_course_json(s3, bucket, args.course_id)
    course_title = (course.get("details") or {}).get("title") or args.course_id
    skipped_no_pdf: list[dict[str, Any]] = []
    if args.all_topics:
        matched, skipped_no_pdf, toc_id = resolve_all_topics(course, args.course_id)
        unmatched = []
        issue_rows = [item["sourceIssueRow"] for item in matched]
        source_workbook = None
        map_topic_rows = None
    else:
        issue_rows, source_workbook, map_topic_rows = load_issue_rows(
            map_path,
            course_title,
            args.map_sheet,
        )
        matched, unmatched, toc_id = resolve_topics(
            issue_rows, course, args.course_id, parse_title_aliases(args.title_alias)
        )
    pipeline = pipeline_fingerprint()
    prior_manifest = _read_json(manifest_path) if args.resume else None
    prior_report = _read_json(report_path) if args.resume else None
    prior_topics = {
        item.get("topicId"): item
        for item in (prior_manifest or {}).get("topics", [])
        if isinstance(item, dict) and item.get("topicId")
    }
    prior_results: dict[str, dict[str, Any]] = {}
    for topic_id, topic in prior_topics.items():
        diagnostics_path = Path(str(topic.get("diagnostics") or ""))
        diagnostics = _read_json(diagnostics_path)
        details = (diagnostics or {}).get("details")
        if isinstance(details, dict):
            prior_results[str(topic_id)] = details
    if not prior_results:
        prior_results = {
            item.get("topicId"): item
            for item in (prior_report or {}).get("results", [])
            if isinstance(item, dict) and item.get("topicId")
        }
    prior_pipeline = (prior_manifest or {}).get("pipelineFingerprint")
    results: list[dict[str, Any]] = []
    compact_topics: list[dict[str, Any]] = []
    counts = {
        "requested": len(issue_rows),
        "matched": len(matched),
        "downloaded": 0,
        "reused": 0,
        "swept": 0,
        "resolved": 0,
        "residual": 0,
        "unverifiable": 0,
        "failed": 0,
        "unmatched": len(unmatched),
    }

    downloads: dict[str, tuple[dict[str, Any], str, bytes]] = {}
    source_etags: dict[str, str] = {}
    pending_downloads: list[dict[str, Any]] = []
    for item in matched:
        topic_id = item["topicId"]
        original_path = original_dir / f"{topic_id}.pdf"
        swept_path = swept_dir / f"{topic_id}.pdf"
        if not args.resume or prior_manifest is None:
            pending_downloads.append(item)
            continue
        try:
            head = s3.head_object(Bucket=bucket, Key=item["pdfKey"])
            etag = str(head.get("ETag") or "").strip('"')
            source_etags[topic_id] = etag
        except Exception as error:
            results.append(
                {
                    **item,
                    "originalPdf": str(original_path),
                    "reSweptPdf": str(swept_path),
                    "status": "failed",
                    "errors": [f"S3 preflight failed: {error}"],
                }
            )
            counts["failed"] += 1
            continue
        prior_topic = prior_topics.get(topic_id)
        if args.resume and _resume_topic(
            prior_topic,
            pipeline=pipeline,
            manifest_pipeline=prior_pipeline,
            source_etag=etag,
        ):
            prior_entry = prior_results.get(topic_id)
            if prior_entry:
                entry = dict(prior_entry)
                entry["reused"] = True
                results.append(entry)
                diagnostics_path = diagnostics_dir / f"{topic_id}.json"
                compact_topics.append(
                    _compact_topic(
                        entry,
                        source_etag=etag,
                        diagnostics_path=diagnostics_path,
                    )
                )
                counts["reused"] += 1
                counts["swept"] += 1
                result_kind = entry.get("resultKind")
                if result_kind in {"resolved", "residual", "unverifiable"}:
                    counts[result_kind] += 1
                continue
        pending_downloads.append(item)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_download_topic, s3, bucket, item): item
            for item in pending_downloads
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                resolved_item, etag, pdf_bytes = future.result()
                downloads[resolved_item["topicId"]] = (
                    resolved_item,
                    etag,
                    pdf_bytes,
                )
                source_etags[resolved_item["topicId"]] = etag
            except Exception as error:
                topic_id = item["topicId"]
                results.append(
                    {
                        **item,
                        "originalPdf": str(original_dir / f"{topic_id}.pdf"),
                        "reSweptPdf": str(swept_dir / f"{topic_id}.pdf"),
                        "status": "failed",
                        "errors": [f"download failed: {error}"],
                    }
                )
                counts["failed"] += 1

    preparation_results, execution_mode = _prepare_downloads(downloads, args.workers)

    for item in matched:
        topic_id = item["topicId"]
        if topic_id not in downloads:
            continue
        resolved_item, etag, pdf_bytes = downloads[topic_id]
        original_path = original_dir / f"{topic_id}.pdf"
        swept_path = swept_dir / f"{topic_id}.pdf"
        entry = {
            **resolved_item,
            "originalPdf": str(original_path),
            "reSweptPdf": str(swept_path),
            "pipelineFingerprint": pipeline,
            "sourceETag": etag,
        }
        try:
            original_path.write_bytes(pdf_bytes)
            counts["downloaded"] += 1
            prepared = preparation_results[topic_id]
            if isinstance(prepared, Exception):
                raise prepared
            swept_bytes, sweep_result = prepared
            swept_path.write_bytes(swept_bytes)
            entry.update(sweep_result)
            counts["swept"] += 1
            entry["residualDiagnostics"] = build_residual_diagnostics(
                sweep_result,
                item["sourceIssueRow"].get("failed_categories"),
                all_categories=args.all_topics,
            )
            result_kind, entry["status"] = classify_residual_status(
                entry["residualDiagnostics"],
                entry["warnings"],
            )
            entry["resultKind"] = result_kind
            counts[result_kind] += 1
            diagnostics_path = diagnostics_dir / f"{topic_id}.json"
            _write_json(
                diagnostics_path,
                {
                    "schemaVersion": 1,
                    "topicId": topic_id,
                    "pipelineFingerprint": pipeline,
                    "details": entry,
                },
            )
            compact_topics.append(
                _compact_topic(
                    entry,
                    source_etag=etag,
                    diagnostics_path=diagnostics_path,
                )
            )
        except Exception as error:
            counts["failed"] += 1
            entry["status"] = "failed"
            entry["errors"] = [str(error)]
        results.append(entry)

    compact_ids = {str(item.get("topicId")) for item in compact_topics}
    for entry in results:
        topic_id = str(entry.get("topicId") or "")
        if not topic_id or topic_id in compact_ids:
            continue
        diagnostics_path = diagnostics_dir / f"{topic_id}.json"
        _write_json(
            diagnostics_path,
            {
                "schemaVersion": 1,
                "topicId": topic_id,
                "pipelineFingerprint": pipeline,
                "details": entry,
            },
        )
        compact_topics.append(
            _compact_topic(
                entry,
                source_etag=source_etags.get(topic_id, ""),
                diagnostics_path=diagnostics_path,
            )
        )

    results.sort(key=lambda item: str(item.get("topicId") or ""))
    compact_topics.sort(key=lambda item: str(item.get("topicId") or ""))
    report = {
        "runAt": run_at.isoformat(),
        "sourceIssueMap": None if args.all_topics else str(map_path.resolve()),
        "topicSelection": "allTopics" if args.all_topics else "issueMap",
        "skippedNoPdf": skipped_no_pdf,
        "sourceWorkbook": source_workbook,
        "course": {
            "selection": course_title,
            "courseId": args.course_id,
            "courseTitle": (course.get("details") or {}).get("title"),
            "environment": args.env,
            "bucket": bucket,
            "defaultTocId": toc_id,
        },
        "pipelineFingerprint": pipeline,
        "execution": {
            "workers": args.workers,
            "mode": execution_mode,
        },
        "scope": {
            "topicRowsOnly": True,
            "issueMapTopicRows": map_topic_rows,
            "selectedCourseSheetRows": len(issue_rows),
            "chapterAggregateRowsDownloaded": 0,
            "pdfPattern": "courses/{courseId}/topic_pdfs/{topicId}.pdf",
        },
        "counts": counts,
        "results": compact_topics,
        "unmatched": unmatched,
        "notes": [
            "Original PDFs are preserved separately from re-swept PDFs.",
            "This is a local generic sweep run; no Adobe checker was run.",
            "Adobe/PDF accessibility conformance remains subject to manual Adobe checking.",
        ],
    }
    _write_json(report_path, report)
    compact_manifest = {
        "schemaVersion": 2,
        "runAt": run_at.isoformat(),
        "pipelineFingerprint": pipeline,
        "execution": report["execution"],
        "course": report["course"],
        "counts": counts,
        "topics": compact_topics,
        "unmatched": unmatched,
        "detailedReport": str(report_path),
        "diagnosticsDir": str(diagnostics_dir),
        "reviewQueue": str(review_path),
    }
    _write_json(manifest_path, compact_manifest)
    review_order = {"residual": 0, "unverifiable": 1, "resolved": 2}
    review_topics = sorted(
        compact_topics,
        key=lambda item: (
            review_order.get(str(item.get("resultKind")), 3),
            str(item.get("topicTitle") or ""),
        ),
    )
    _write_json(
        review_path,
        {
            "schemaVersion": 1,
            "courseId": args.course_id,
            "manifest": str(manifest_path),
            "topics": [
                {
                    "topicId": topic.get("topicId"),
                    "topicTitle": topic.get("topicTitle"),
                    "status": topic.get("status"),
                    "resultKind": topic.get("resultKind"),
                    "reSweptPdf": topic.get("reSweptPdf"),
                    "residualCategories": topic.get("residualCategories"),
                }
                for topic in review_topics
            ],
        },
    )
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "reviewQueue": str(review_path),
                "counts": counts,
            },
            separators=(",", ":"),
        )
    )
    return 1 if counts["failed"] or counts["unmatched"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
