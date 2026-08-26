#!/usr/bin/env python3
"""One-time Channels topic preview a11y migration.

Remediates topic_pdfs/{topicId}.pdf via the Adobe accessibility pipeline.
Topic download and chapter worksheets are rebuilt separately via generate-pdf-lambda.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import boto3

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from lib.channels_paths import (  # noqa: E402
    ChapterScope,
    TopicScope,
    resolve_auto_chapter_plan,
    resolve_migration_scope,
)
from lib.cloudfront import invalidate_paths  # noqa: E402
from lib.config import (  # noqa: E402
    a11y_bucket,
    channels_bucket,
    cloudfront_distribution_id,
    state_machine_arn,
)
from lib.figure_alt_sweep import repair_missing_figure_alt  # noqa: E402
from lib.figure_to_table_sweep import repair_figure_to_table  # noqa: E402
from lib.inline_formula_sweep import repair_inline_formula_figures  # noqa: E402
from lib.layout_table_sweep import repair_layout_tables  # noqa: E402
from lib.character_encoding_sweep import repair_character_encoding  # noqa: E402
from lib.heading_nesting_sweep import repair_heading_nesting  # noqa: E402
from lib.marked_content_actualtext_sweep import repair_marked_content_actualtext  # noqa: E402
from lib.tab_order_sweep import repair_tab_order  # noqa: E402
from lib.tagged_annotation_sweep import repair_tagged_annotations  # noqa: E402
from lib.tagged_content_sweep import repair_tagged_content  # noqa: E402
from lib.bookmark_sweep import repair_bookmarks  # noqa: E402
from lib.adobe_residual_report import load_adobe_residual_telemetry  # noqa: E402
from lib.migration_progress import (  # noqa: E402
    clear_topic_failed,
    completed_topic_ids,
    load_progress,
    mark_chapter_completed,
    mark_topic_completed,
    mark_topic_failed,
    pending_failed_topic_ids,
    resolve_allowed_chapter_indices,
    save_progress,
)
from lib.pdf_a11y_audit import audit_pdf_bytes, is_likely_remediated  # noqa: E402
from lib.remediation import remediate_preview_pdf  # noqa: E402

DEFAULT_TIME_BUDGET_SECONDS = 6 * 3600 + 30 * 60  # 6.5 hours (CodeBuild max is 8h)


@dataclass
class TopicRunResult:
    topic_id: str
    status: str
    audit: dict | None = None
    preview_key: str | None = None
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remediate Channels topic preview PDFs (topic_pdfs/{topicId}.pdf)"
    )
    parser.add_argument("--course-id", required=True)
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    parser.add_argument("--toc-id", default=None, help="Reference TOC for --auto-chapters")
    parser.add_argument(
        "--chapter-id",
        default=None,
        help="If set, only remediate preview PDFs for topics in this chapter",
    )
    parser.add_argument(
        "--auto-chapters",
        action="store_true",
        help="Walk chapters from course default TOC (or --toc-id), with S3 progress",
    )
    parser.add_argument(
        "--chapter-ids",
        default=None,
        help="Auto-chapters: comma-separated chapter IDs to process (TOC order)",
    )
    parser.add_argument(
        "--topic-ids",
        default=None,
        help="Comma-separated topic IDs to remediate (filters chapter/course scope)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-cdn-invalidation", action="store_true")
    parser.add_argument(
        "--skip-if-audited",
        action="store_true",
        help="Skip topics whose preview already passes local tagged/alt audit",
    )
    parser.add_argument(
        "--time-budget-seconds",
        type=int,
        default=DEFAULT_TIME_BUDGET_SECONDS,
        help="Stop after this many seconds and save progress (auto-chapters only)",
    )
    parser.add_argument(
        "--allow-missing-figure-alt",
        action="store_true",
        help="Deprecated compatibility flag; publication is always nonblocking (reported in migration JSON)",
    )
    parser.add_argument(
        "--allow-suspicious-figure-alt",
        action="store_true",
        help="Deprecated compatibility flag; publication is always nonblocking (reported in migration JSON)",
    )
    parser.add_argument(
        "--no-repair-missing-figure-alt",
        action="store_true",
        help="Skip post-remediation fallback /Alt on figures missing alt text",
    )
    parser.add_argument(
        "--no-repair-layout-tables",
        action="store_true",
        help="Skip post-remediation unwrap/repair of layout /Table regions",
    )
    parser.add_argument(
        "--repair-figure-to-table",
        action="store_true",
        help="Opt-in: retag table-like /Figure elements as /Table (default keeps /Figure)",
    )
    parser.add_argument(
        "--no-repair-figure-to-table",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-repair-inline-formula",
        action="store_true",
        help="Skip expanding caption-only inline-formula /Figure alt text",
    )
    parser.add_argument(
        "--no-repair-character-encoding",
        action="store_true",
        help="Skip /ActualText and /ToUnicode repairs for character encoding failures",
    )
    parser.add_argument(
        "--no-repair-marked-content-actualtext",
        action="store_true",
        help="Skip /ActualText injection for table and inline-formula marked content",
    )
    return parser.parse_args()


def print_scope(topics: list[TopicScope], chapter_id: str | None) -> None:
    print(f"Topic previews in scope: {len(topics)}")
    if chapter_id:
        print(f"Chapter filter: {chapter_id}")
    for topic in topics:
        print(f"  - {topic.topic_id}: {topic.title}")
        print(f"      preview: {topic.preview_key}")


def filter_blocking_suspicious_figure_alts(
    suspicious: list,
    *,
    marked_content_actions: list[str] | None,
) -> list:
    """Drop table-as-figure flags when /ActualText was injected for that figure."""
    if not marked_content_actions:
        return suspicious

    actualtext_figures: set[int] = set()
    for action in marked_content_actions:
        match = re.match(r"figure(\d+): added /ActualText", action)
        if match:
            actualtext_figures.add(int(match.group(1)))

    if not actualtext_figures:
        return suspicious

    filtered: list = []
    for item in suspicious:
        if item.figure_index in actualtext_figures:
            continue
        filtered.append(item)
    return filtered


def filter_topics_by_ids(topics: list[TopicScope], topic_ids: str | None) -> list[TopicScope]:
    if not topic_ids:
        return topics
    allowed = {topic_id.strip() for topic_id in topic_ids.split(",") if topic_id.strip()}
    filtered = [topic for topic in topics if topic.topic_id in allowed]
    missing = allowed - {topic.topic_id for topic in filtered}
    if missing:
        print(f"Warning: topic IDs not in scope or missing preview: {sorted(missing)}")
    return filtered


def remediate_single_topic(
    s3,
    stepfunctions,
    a11y: str,
    sm_arn: str,
    course_id: str,
    topic: TopicScope,
    args: argparse.Namespace,
    preview_bytes: bytes | None = None,
) -> TopicRunResult:
    try:
        if preview_bytes is None:
            preview_bytes = s3.get_object(Bucket=channels_bucket(args.env), Key=topic.preview_key)[
                "Body"
            ].read()

        if args.skip_if_audited:
            try:
                already_remediated = is_likely_remediated(preview_bytes)
            except Exception as error:
                already_remediated = False
                print(
                    "    WARN: skip-if-audited check failed; "
                    f"continuing with remediation: {error}",
                    flush=True,
                )
            if already_remediated:
                audit = audit_pdf_bytes(preview_bytes)
                adobe_telemetry = load_adobe_residual_telemetry(
                    s3, a11y, topic.topic_id
                ).to_dict()
                print("    SKIP already remediated (local audit)")
                return TopicRunResult(
                    topic_id=topic.topic_id,
                    status="skipped-audited",
                    audit={
                        **audit.to_dict(),
                        "adobeResidualTelemetry": adobe_telemetry,
                        "publicationBlocked": False,
                        "publicationPolicy": "best-effort-nonblocking",
                    },
                    preview_key=topic.preview_key,
                )

        remediated = remediate_preview_pdf(
            s3,
            stepfunctions,
            a11y,
            sm_arn,
            preview_bytes,
            course_id,
            topic.topic_id,
            topic.title,
        )
        tab_order_repair = None
        tab_order_sweep_error = None
        try:
            remediated, tab_order_repair = repair_tab_order(remediated)
            if tab_order_repair.actions:
                for action in tab_order_repair.actions:
                    print(f"    {action}")
        except Exception as error:
            tab_order_sweep_error = str(error)
            print(
                f"    WARN: tab-order sweep failed; continuing with pre-sweep bytes: {error}",
                flush=True,
            )
        tagged_content_repair = None
        tagged_content_sweep_error = None
        tagged_content_before = remediated
        try:
            remediated, tagged_content_repair = repair_tagged_content(remediated)
            if tagged_content_repair.actions:
                for action in tagged_content_repair.actions:
                    print(f"    {action}")
        except Exception as error:
            remediated = tagged_content_before
            tagged_content_sweep_error = str(error)
            print(
                f"    WARN: tagged-content sweep failed; continuing with pre-sweep bytes: {error}",
                flush=True,
            )
        tagged_annotation_repair = None
        tagged_annotation_sweep_error = None
        tagged_annotation_before = remediated
        try:
            remediated, tagged_annotation_repair = repair_tagged_annotations(remediated)
            if tagged_annotation_repair.actions:
                for action in tagged_annotation_repair.actions:
                    print(f"    {action}")
            if tagged_annotation_repair.conflicts or tagged_annotation_repair.unresolved:
                print(
                    "    tagged-annotation sweep diagnostics: "
                    f"{len(tagged_annotation_repair.conflicts)} conflict(s), "
                    f"{len(tagged_annotation_repair.unresolved)} unresolved",
                    flush=True,
                )
        except Exception as error:
            remediated = tagged_annotation_before
            tagged_annotation_sweep_error = str(error)
            print(
                "    WARN: tagged-annotation sweep failed; "
                f"continuing with pre-sweep bytes: {error}",
                flush=True,
            )
        audit_before = None
        audit_before_error = None
        try:
            audit_before = audit_pdf_bytes(remediated)
        except Exception as error:
            audit_before_error = str(error)
            print(
                f"    WARN: pre-sweep local audit failed; continuing: {error}",
                flush=True,
            )
        repaired_figures: list[int] = []
        figure_alt_sweep_error = None
        if (
            audit_before
            and audit_before.figures_missing_alt
            and not args.no_repair_missing_figure_alt
        ):
            figure_alt_before = remediated
            try:
                remediated, repaired_figures = repair_missing_figure_alt(remediated)
                if repaired_figures:
                    print(f"    repaired figure alt: {repaired_figures}")
            except Exception as error:
                remediated = figure_alt_before
                figure_alt_sweep_error = str(error)
                print(
                    "    WARN: figure-alt sweep failed; "
                    f"continuing with pre-sweep bytes: {error}",
                    flush=True,
                )

        table_repair = None
        table_sweep_error = None
        figure_to_table_repair = None
        figure_to_table_sweep_error = None
        inline_formula_repair = None
        inline_formula_sweep_error = None
        marked_content_repair = None
        marked_content_sweep_error = None
        character_encoding_repair = None
        if args.repair_figure_to_table and not args.no_repair_figure_to_table:
            figure_to_table_before = remediated
            try:
                remediated, figure_to_table_repair = repair_figure_to_table(remediated)
                if figure_to_table_repair.actions:
                    for action in figure_to_table_repair.actions:
                        print(f"    {action}")
            except Exception as error:
                remediated = figure_to_table_before
                figure_to_table_sweep_error = str(error)
                print(
                    "    WARN: figure-to-table sweep failed; "
                    f"continuing with pre-sweep bytes: {error}",
                    flush=True,
                )

        if not args.no_repair_inline_formula:
            inline_formula_before = remediated
            try:
                remediated, inline_formula_repair = repair_inline_formula_figures(
                    remediated
                )
                if inline_formula_repair.actions:
                    for action in inline_formula_repair.actions:
                        print(f"    {action}")
            except Exception as error:
                remediated = inline_formula_before
                inline_formula_sweep_error = str(error)
                print(
                    "    WARN: inline-formula sweep failed; "
                    f"continuing with pre-sweep bytes: {error}",
                    flush=True,
                )

        if not args.no_repair_layout_tables:
            table_before = remediated
            try:
                remediated, table_repair = repair_layout_tables(remediated)
                if table_repair.actions:
                    for action in table_repair.actions:
                        print(f"    {action}")
                if table_repair.conflicts or table_repair.unresolved:
                    print(
                        f"    table sweep diagnostics: "
                        f"{len(table_repair.conflicts)} conflict(s), "
                        f"{len(table_repair.unresolved)} unresolved",
                        flush=True,
                    )
            except Exception as error:
                remediated = table_before
                table_sweep_error = str(error)
                print(
                    f"    WARN: table sweep failed; continuing with pre-sweep bytes: {error}",
                    flush=True,
                )

        if not args.no_repair_marked_content_actualtext:
            marked_content_before = remediated
            try:
                remediated, marked_content_repair = repair_marked_content_actualtext(
                    remediated
                )
                if marked_content_repair.actions:
                    for action in marked_content_repair.actions:
                        print(f"    {action}")
            except Exception as error:
                remediated = marked_content_before
                marked_content_sweep_error = str(error)
                print(
                    "    WARN: marked-content /ActualText sweep failed; "
                    f"continuing with pre-sweep bytes: {error}",
                    flush=True,
                )

        character_encoding_sweep_error = None
        if not args.no_repair_character_encoding:
            character_encoding_before = remediated
            try:
                remediated, character_encoding_repair = repair_character_encoding(
                    remediated
                )
                if character_encoding_repair.actions:
                    for action in character_encoding_repair.actions:
                        print(f"    {action}")
                if character_encoding_repair.diagnostics:
                    print(
                        "    encoding sweep diagnostics: "
                        f"{len(character_encoding_repair.diagnostics)} item(s)",
                        flush=True,
                    )
            except Exception as error:
                remediated = character_encoding_before
                character_encoding_sweep_error = str(error)
                print(
                    "    WARN: character-encoding sweep failed; "
                    f"continuing with pre-sweep bytes: {error}",
                    flush=True,
                )

        heading_nesting_repair = None
        heading_nesting_sweep_error = None
        heading_nesting_before = remediated
        try:
            remediated, heading_nesting_repair = repair_heading_nesting(remediated)
            if heading_nesting_repair.actions:
                for action in heading_nesting_repair.actions:
                    print(f"    {action}")
            if heading_nesting_repair.diagnostics:
                print(
                    "    heading-nesting sweep diagnostics: "
                    f"{len(heading_nesting_repair.diagnostics)} item(s)",
                    flush=True,
                )
        except Exception as error:
            remediated = heading_nesting_before
            heading_nesting_sweep_error = str(error)
            print(
                "    WARN: heading-nesting sweep failed; "
                f"continuing with pre-sweep bytes: {error}",
                flush=True,
            )

        bookmark_repair = None
        bookmark_sweep_error = None
        bookmark_before = remediated
        try:
            remediated, bookmark_repair = repair_bookmarks(remediated)
            if bookmark_repair.actions:
                for action in bookmark_repair.actions:
                    print(f"    {action}")
            if bookmark_repair.conflicts:
                print(
                    "    bookmark sweep diagnostics: "
                    f"{len(bookmark_repair.conflicts)} conflict(s)",
                    flush=True,
                )
        except Exception as error:
            remediated = bookmark_before
            bookmark_sweep_error = str(error)
            print(
                "    WARN: bookmark sweep failed; continuing with pre-sweep bytes: "
                f"{error}",
                flush=True,
            )

        audit = None
        audit_error = None
        try:
            audit = audit_pdf_bytes(remediated)
        except Exception as error:
            audit_error = str(error)
            print(
                f"    WARN: final local audit failed; publishing anyway: {error}",
                flush=True,
            )
        blocking_suspicious = (
            filter_blocking_suspicious_figure_alts(
                audit.figures_suspicious_alt,
                marked_content_actions=(
                    marked_content_repair.actions if marked_content_repair else None
                ),
            )
            if audit
            else []
        )
        adobe_telemetry = load_adobe_residual_telemetry(
            s3, a11y, topic.topic_id
        ).to_dict()
        publication_warnings: list[str] = []
        if audit and audit.figures_missing_alt:
            publication_warnings.append(
                f"figures missing /Alt: {audit.figures_missing_alt}"
            )
        if audit and audit.figures_suspicious_alt:
            publication_warnings.append(
                "figures with suspicious /Alt: "
                f"{[item.figure_index for item in audit.figures_suspicious_alt]}"
            )
        if audit_error:
            publication_warnings.append(f"final local audit failed: {audit_error}")
        audit_payload = {
            **(audit.to_dict() if audit else {}),
            "beforeRepair": audit_before.to_dict() if audit_before else None,
            "beforeRepairAuditError": audit_before_error,
            "finalAuditError": audit_error,
            "blockingSuspiciousFigureAlt": [
                item.to_dict() for item in blocking_suspicious
            ],
            "repairedFigures": repaired_figures,
            "figureAltSweepError": figure_alt_sweep_error,
            "figureToTableRepair": (
                figure_to_table_repair.to_dict() if figure_to_table_repair else None
            ),
            "figureToTableSweepError": figure_to_table_sweep_error,
            "inlineFormulaRepair": (
                inline_formula_repair.to_dict() if inline_formula_repair else None
            ),
            "inlineFormulaSweepError": inline_formula_sweep_error,
            "markedContentRepair": (
                marked_content_repair.to_dict() if marked_content_repair else None
            ),
            "markedContentSweepError": marked_content_sweep_error,
            "characterEncodingRepair": (
                character_encoding_repair.to_dict()
                if character_encoding_repair
                else None
            ),
            "characterEncodingSweepError": character_encoding_sweep_error,
            "headingNestingRepair": (
                heading_nesting_repair.to_dict()
                if heading_nesting_repair
                else None
            ),
            "headingNestingSweepError": heading_nesting_sweep_error,
            "bookmarkRepair": (
                bookmark_repair.to_dict() if bookmark_repair else None
            ),
            "bookmarkSweepError": bookmark_sweep_error,
            "tableRepair": table_repair.to_dict() if table_repair else None,
            "tableSweepError": table_sweep_error,
            "tabOrderRepair": (
                tab_order_repair.to_dict() if tab_order_repair else None
            ),
            "tabOrderSweepError": tab_order_sweep_error,
            "taggedContentRepair": (
                tagged_content_repair.to_dict() if tagged_content_repair else None
            ),
            "taggedContentSweepError": tagged_content_sweep_error,
            "taggedAnnotationRepair": (
                tagged_annotation_repair.to_dict()
                if tagged_annotation_repair
                else None
            ),
            "taggedAnnotationSweepError": tagged_annotation_sweep_error,
            "adobeResidualTelemetry": adobe_telemetry,
            "publicationBlocked": False,
            "publicationPolicy": "best-effort-nonblocking",
            "publicationWarnings": publication_warnings,
        }
        if audit:
            print(
                f"    audit: {audit.figure_count} figures, "
                f"{len(audit.figures_missing_alt)} missing alt, "
                f"{len(audit.figures_suspicious_alt)} suspicious alt, "
                f"{audit.table_count} tables"
            )
        if audit and audit.figures_suspicious_alt:
            for item in audit.figures_suspicious_alt:
                print(
                    f"    suspicious figure {item.figure_index}: "
                    f"{', '.join(item.reasons)} — {item.alt_text[:80]!r}"
                )
        s3.put_object(
            Bucket=channels_bucket(args.env),
            Key=topic.preview_key,
            Body=remediated,
            ContentType="application/pdf",
        )
        print("    OK preview replaced")
        return TopicRunResult(
            topic_id=topic.topic_id,
            status="remediated",
            audit=audit_payload,
            preview_key=topic.preview_key,
        )
    except Exception as error:
        print(f"    FAILED {topic.topic_id}: {error}")
        return TopicRunResult(
            topic_id=topic.topic_id,
            status="failed",
            error=str(error),
            preview_key=topic.preview_key,
        )


def run_topic_batch(
    s3,
    stepfunctions,
    a11y: str,
    sm_arn: str,
    course_id: str,
    topics: list[TopicScope],
    args: argparse.Namespace,
    *,
    deadline: float | None = None,
) -> tuple[list[TopicRunResult], bool]:
    results: list[TopicRunResult] = []
    stopped_early = False

    for topic in topics:
        if deadline is not None and time.monotonic() >= deadline:
            print("Time budget reached — stopping batch.")
            stopped_early = True
            break

        print(f"  Adobe pass: {topic.topic_id} ({topic.title})")
        result = remediate_single_topic(
            s3, stepfunctions, a11y, sm_arn, course_id, topic, args
        )
        results.append(result)

    return results, stopped_early


def print_auto_chapter_plan(
    s3,
    channels: str,
    chapters: list[ChapterScope],
    reference_toc_id: str,
    progress: dict,
    args: argparse.Namespace,
    allowed_indices: set[int] | None = None,
) -> None:
    done = completed_topic_ids(progress)
    print(f"Auto-chapters plan (reference TOC: {reference_toc_id})")
    print(f"Chapters with PDF topics: {len(chapters)}")
    print(f"Progress: {len(progress.get('completedChapters') or [])} chapters, {len(done)} topics")
    print(f"Next chapter index: {progress.get('nextChapterIndex', 0)}")

    orphan_total = 0
    for index, chapter in enumerate(chapters):
        pending = [topic for topic in chapter.topics if topic.topic_id not in done]
        skipped = 0
        if args.skip_if_audited and pending:
            for topic in pending:
                preview_bytes = s3.get_object(Bucket=channels, Key=topic.preview_key)["Body"].read()
                if is_likely_remediated(preview_bytes):
                    skipped += 1
        marker = " (next)" if index == int(progress.get("nextChapterIndex") or 0) else ""
        if chapter.chapter_id in (progress.get("completedChapters") or []):
            marker = " (done)"
        if allowed_indices is not None and index not in allowed_indices:
            marker = " (out of scope)"
        print(
            f"  [{index}] {chapter.chapter_id}: {chapter.title} — "
            f"{len(pending)} pending, {skipped} would skip-audit{marker}"
        )

    course_topics = resolve_migration_scope(s3, channels, args.course_id)
    orphan_total = sum(1 for topic in course_topics if topic.topic_id not in done)
    toc_topic_ids = {topic.topic_id for chapter in chapters for topic in chapter.topics}
    orphans = [topic for topic in course_topics if topic.topic_id not in toc_topic_ids]
    if orphans:
        print(f"Orphan topics (not in reference TOC chapters): {len(orphans)}")
        for topic in orphans[:10]:
            print(f"  - {topic.topic_id}: {topic.title}")
        if len(orphans) > 10:
            print(f"  ... and {len(orphans) - 10} more")
    print(f"Course-wide pending topics (incl. orphans): {orphan_total}")
    failed_retry = pending_failed_topic_ids(progress)
    if failed_retry:
        print(f"Failed-topic retry pass: {len(failed_retry)} topic(s) pending")


def build_topic_chapter_lookup(
    chapters: list[ChapterScope],
) -> tuple[dict[str, TopicScope], dict[str, tuple[int, ChapterScope]]]:
    topics_by_id: dict[str, TopicScope] = {}
    chapter_by_topic: dict[str, tuple[int, ChapterScope]] = {}
    for index, chapter in enumerate(chapters):
        for topic in chapter.topics:
            topics_by_id[topic.topic_id] = topic
            chapter_by_topic[topic.topic_id] = (index, chapter)
    return topics_by_id, chapter_by_topic


def reconcile_chapter_if_complete(
    progress: dict,
    chapter_index: int,
    chapter: ChapterScope,
) -> None:
    done = completed_topic_ids(progress)
    if all(topic.topic_id in done for topic in chapter.topics):
        if chapter.chapter_id not in (progress.get("completedChapters") or []):
            mark_chapter_completed(progress, chapter.chapter_id, chapter_index)


def save_topic_progress(
    s3,
    bucket: str,
    course_id: str,
    progress: dict,
    run_id: str,
    *,
    done_count: int,
    total_topics: int,
    topic_id: str,
    status: str,
    chapter_index: int | None = None,
) -> None:
    if chapter_index is not None:
        progress["currentChapterIndex"] = chapter_index
    progress["currentTopicId"] = topic_id
    save_progress(s3, bucket, course_id, progress, run_id)
    print(
        f"    progress: {done_count}/{total_topics} topics ({status})",
        flush=True,
    )


def run_auto_chapters(
    s3,
    stepfunctions,
    a11y: str,
    sm_arn: str,
    distribution_id: str,
    args: argparse.Namespace,
) -> int:
    channels = channels_bucket(args.env)
    reference_toc_id, chapters = resolve_auto_chapter_plan(
        s3, channels, args.course_id, args.toc_id
    )
    progress = load_progress(s3, channels, args.course_id, reference_toc_id, args.env)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    done = completed_topic_ids(progress)
    chapter_id_to_index = {chapter.chapter_id: index for index, chapter in enumerate(chapters)}
    allowed_indices: set[int] | None = None
    if args.chapter_ids:
        try:
            allowed_indices = resolve_allowed_chapter_indices(
                chapter_id_to_index,
                args.chapter_ids,
            )
        except ValueError as error:
            print(error)
            return 1

    if args.dry_run:
        print_auto_chapter_plan(
            s3,
            channels,
            chapters,
            reference_toc_id,
            progress,
            args,
            allowed_indices,
        )
        print("Dry run complete — no writes performed.")
        return 0

    deadline = time.monotonic() + args.time_budget_seconds
    paths_overwritten: list[str] = []
    topic_results: dict[str, dict] = {}
    topic_failures: list[str] = []
    stopped_early = False
    chapters_processed = 0

    start_index = int(progress.get("nextChapterIndex") or 0)
    total_topics = sum(len(chapter.topics) for chapter in chapters)
    print(
        f"Auto-chapters: {len(chapters)} chapters, {total_topics} topics, "
        f"starting at index {start_index}, time budget {args.time_budget_seconds}s",
        flush=True,
    )
    if allowed_indices is not None:
        in_scope = sorted(allowed_indices)
        print(
            f"Chapter filter: {len(in_scope)} chapter(s) — "
            f"indices {in_scope[0]}-{in_scope[-1]}",
            flush=True,
        )

    chapter_walk_finished = False

    for index in range(start_index, len(chapters)):
        if allowed_indices is not None and index not in allowed_indices:
            continue
        if time.monotonic() >= deadline:
            print(f"Time budget reached before chapter index {index}.")
            progress["nextChapterIndex"] = index
            stopped_early = True
            save_progress(s3, channels, args.course_id, progress, run_id)
            break

        chapter = chapters[index]
        pending_topics = [topic for topic in chapter.topics if topic.topic_id not in done]
        if not pending_topics:
            if chapter.chapter_id not in (progress.get("completedChapters") or []):
                mark_chapter_completed(progress, chapter.chapter_id, index)
                save_progress(s3, channels, args.course_id, progress, run_id)
            print(f"Chapter [{index}] {chapter.title} — already complete, skipping")
            continue

        print(
            f"Chapter [{index}] {chapter.chapter_id}: {chapter.title} "
            f"({len(pending_topics)} topic(s))",
            flush=True,
        )
        chapter_failures: list[str] = []
        for topic in pending_topics:
            if time.monotonic() >= deadline:
                print("Time budget reached mid-chapter.", flush=True)
                progress["nextChapterIndex"] = index
                stopped_early = True
                break

            print(f"  Adobe pass: {topic.topic_id} ({topic.title})", flush=True)
            result = remediate_single_topic(
                s3, stepfunctions, a11y, sm_arn, args.course_id, topic, args
            )
            topic_results[topic.topic_id] = {
                "status": result.status,
                "audit": result.audit,
                "error": result.error,
                "chapterId": chapter.chapter_id,
            }

            if result.status == "failed":
                chapter_failures.append(topic.topic_id)
                topic_failures.append(topic.topic_id)
                mark_topic_failed(progress, topic.topic_id)
                save_topic_progress(
                    s3,
                    channels,
                    args.course_id,
                    progress,
                    run_id,
                    done_count=len(done),
                    total_topics=total_topics,
                    topic_id=topic.topic_id,
                    status="failed",
                    chapter_index=index,
                )
                continue

            clear_topic_failed(progress, topic.topic_id)

            skipped_reason = "skip-if-audited" if result.status == "skipped-audited" else None
            mark_topic_completed(progress, topic.topic_id, skipped_reason=skipped_reason)
            done.add(topic.topic_id)
            if result.preview_key and result.status == "remediated":
                paths_overwritten.append(result.preview_key)
            save_topic_progress(
                s3,
                channels,
                args.course_id,
                progress,
                run_id,
                done_count=len(done),
                total_topics=total_topics,
                topic_id=topic.topic_id,
                status=result.status,
                chapter_index=index,
            )

        if stopped_early:
            save_progress(s3, channels, args.course_id, progress, run_id)
            break

        if chapter_failures:
            current_next = int(progress.get("nextChapterIndex") or 0)
            progress["nextChapterIndex"] = min(current_next, index)
            save_progress(s3, channels, args.course_id, progress, run_id)
            print(
                f"Chapter {chapter.chapter_id} had {len(chapter_failures)} failure(s) "
                f"— not marking chapter complete, continuing to next chapter",
                flush=True,
            )
            continue

        mark_chapter_completed(progress, chapter.chapter_id, index)
        chapters_processed += 1
        save_progress(s3, channels, args.course_id, progress, run_id)
        print(f"Chapter [{index}] complete — progress saved")
    else:
        chapter_walk_finished = True
        if not stopped_early:
            print("Chapter walk finished for this run.", flush=True)

    if not stopped_early and chapter_walk_finished and allowed_indices is None:
        course_topics = resolve_migration_scope(s3, channels, args.course_id)
        toc_topic_ids = {topic.topic_id for chapter in chapters for topic in chapter.topics}
        orphan_topics = [
            topic
            for topic in course_topics
            if topic.topic_id not in toc_topic_ids and topic.topic_id not in done
        ]
        if orphan_topics:
            print(
                f"Orphan pass: {len(orphan_topics)} topic(s) outside reference TOC chapters",
                flush=True,
            )
            for topic in orphan_topics:
                if time.monotonic() >= deadline:
                    stopped_early = True
                    save_progress(s3, channels, args.course_id, progress, run_id)
                    break
                print(f"  Adobe pass: {topic.topic_id} ({topic.title})", flush=True)
                result = remediate_single_topic(
                    s3, stepfunctions, a11y, sm_arn, args.course_id, topic, args
                )
                topic_results[topic.topic_id] = {
                    "status": result.status,
                    "audit": result.audit,
                    "error": result.error,
                    "chapterId": None,
                }
                if result.status == "failed":
                    topic_failures.append(topic.topic_id)
                    mark_topic_failed(progress, topic.topic_id)
                    save_topic_progress(
                        s3,
                        channels,
                        args.course_id,
                        progress,
                        run_id,
                        done_count=len(done),
                        total_topics=total_topics,
                        topic_id=topic.topic_id,
                        status="failed",
                    )
                    continue
                clear_topic_failed(progress, topic.topic_id)
                skipped_reason = "skip-if-audited" if result.status == "skipped-audited" else None
                mark_topic_completed(progress, topic.topic_id, skipped_reason=skipped_reason)
                done.add(topic.topic_id)
                if result.preview_key and result.status == "remediated":
                    paths_overwritten.append(result.preview_key)
                save_topic_progress(
                    s3,
                    channels,
                    args.course_id,
                    progress,
                    run_id,
                    done_count=len(done),
                    total_topics=total_topics,
                    topic_id=topic.topic_id,
                    status=result.status,
                )

    if not stopped_early and chapter_walk_finished:
        failed_retry_ids = pending_failed_topic_ids(progress)
        if failed_retry_ids:
            topics_by_id, chapter_by_topic = build_topic_chapter_lookup(chapters)
            if allowed_indices is not None:
                failed_retry_ids = [
                    topic_id
                    for topic_id in failed_retry_ids
                    if topic_id in chapter_by_topic
                    and chapter_by_topic[topic_id][0] in allowed_indices
                ]
            course_topics_map = {
                topic.topic_id: topic
                for topic in resolve_migration_scope(s3, channels, args.course_id)
            }
            if failed_retry_ids:
                print(
                    f"Failed-topic retry pass: {len(failed_retry_ids)} topic(s)",
                    flush=True,
                )
            for topic_id in failed_retry_ids:
                    if time.monotonic() >= deadline:
                        print("Time budget reached during failed-topic retry.", flush=True)
                        stopped_early = True
                        save_progress(s3, channels, args.course_id, progress, run_id)
                        break

                    topic = topics_by_id.get(topic_id) or course_topics_map.get(topic_id)
                    if not topic:
                        print(
                            f"  WARN: failed topic {topic_id} not in scope, skipping",
                            flush=True,
                        )
                        continue

                    chapter_info = chapter_by_topic.get(topic_id)
                    chapter_index = chapter_info[0] if chapter_info else None
                    chapter = chapter_info[1] if chapter_info else None
                    chapter_id = chapter.chapter_id if chapter else None

                    print(
                        f"  Adobe pass (retry): {topic.topic_id} ({topic.title})",
                        flush=True,
                    )
                    result = remediate_single_topic(
                        s3, stepfunctions, a11y, sm_arn, args.course_id, topic, args
                    )
                    topic_results[topic.topic_id] = {
                        "status": result.status,
                        "audit": result.audit,
                        "error": result.error,
                        "chapterId": chapter_id,
                        "retry": True,
                    }

                    if result.status == "failed":
                        topic_failures.append(topic.topic_id)
                        mark_topic_failed(progress, topic.topic_id)
                        save_topic_progress(
                            s3,
                            channels,
                            args.course_id,
                            progress,
                            run_id,
                            done_count=len(done),
                            total_topics=total_topics,
                            topic_id=topic.topic_id,
                            status="failed",
                            chapter_index=chapter_index,
                        )
                        continue

                    clear_topic_failed(progress, topic.topic_id)
                    skipped_reason = (
                        "skip-if-audited" if result.status == "skipped-audited" else None
                    )
                    mark_topic_completed(progress, topic.topic_id, skipped_reason=skipped_reason)
                    done.add(topic.topic_id)
                    if result.preview_key and result.status == "remediated":
                        paths_overwritten.append(result.preview_key)
                    save_topic_progress(
                        s3,
                        channels,
                        args.course_id,
                        progress,
                        run_id,
                        done_count=len(done),
                        total_topics=total_topics,
                        topic_id=topic.topic_id,
                        status=result.status,
                        chapter_index=chapter_index,
                    )
                    if chapter is not None and chapter_index is not None:
                        reconcile_chapter_if_complete(progress, chapter_index, chapter)
                        save_progress(s3, channels, args.course_id, progress, run_id)

    invalidation_id = None
    if not args.skip_cdn_invalidation and paths_overwritten:
        cdn_paths = [f"/{path}" for path in paths_overwritten]
        invalidation_id = invalidate_paths(distribution_id, cdn_paths)
        print(f"CloudFront invalidation: {invalidation_id or 'skipped'}")

    report = {
        "runId": run_id,
        "scope": "auto-chapters",
        "mode": "preview-only",
        "courseId": args.course_id,
        "tocId": reference_toc_id,
        "env": args.env,
        "dryRun": False,
        "autoChapters": True,
        "chapterIds": args.chapter_ids,
        "chaptersProcessedThisRun": chapters_processed,
        "stoppedEarly": stopped_early,
        "timeBudgetSeconds": args.time_budget_seconds,
        "progressKey": f"courses/{args.course_id}/.a11y-migration-progress.json",
        "topics": {
            "results": topic_results,
            "failed": topic_failures,
        },
        "pathsOverwritten": paths_overwritten,
        "cdnInvalidation": {
            "distributionId": distribution_id,
            "invalidationId": invalidation_id,
        },
        "progressSnapshot": {
            "completedChapters": progress.get("completedChapters"),
            "completedTopicsCount": len(progress.get("completedTopics") or []),
            "nextChapterIndex": progress.get("nextChapterIndex"),
        },
    }

    reports_dir = Path(__file__).resolve().parents[1] / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / f"migrate-{run_id}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report: {report_path}")

    if stopped_early:
        print("Run stopped early — re-run the same build to continue from saved progress.")
        return 0
    if topic_failures:
        print(
            f"Migration finished with {len(topic_failures)} topic failure(s): "
            f"{', '.join(topic_failures)}",
            flush=True,
        )
        print("Re-run the same build to retry failed topics.", flush=True)
        return 0
    print("Auto-chapters migration complete for course scope.")
    return 0


def main() -> int:
    args = parse_args()
    if args.auto_chapters and args.chapter_id:
        print("Use either --auto-chapters or --chapter-id, not both.")
        return 1
    if args.chapter_ids and not args.auto_chapters:
        print("--chapter-ids requires --auto-chapters.")
        return 1

    s3 = boto3.client("s3")
    stepfunctions = boto3.client("stepfunctions")
    a11y = a11y_bucket()
    sm_arn = state_machine_arn()
    distribution_id = cloudfront_distribution_id(args.env)

    if args.auto_chapters:
        return run_auto_chapters(s3, stepfunctions, a11y, sm_arn, distribution_id, args)

    channels = channels_bucket(args.env)
    topics = resolve_migration_scope(
        s3,
        channels,
        args.course_id,
        chapter_id=args.chapter_id,
        toc_id=args.toc_id,
    )
    topics = filter_topics_by_ids(topics, args.topic_ids)

    if not topics:
        print("Nothing to migrate for the requested scope.")
        return 1

    print_scope(topics, args.chapter_id)
    if args.dry_run:
        print("Dry run complete — no writes performed.")
        print("Live run would remediate preview PDFs, repair missing figure alt, and audit.")
        return 0

    paths_overwritten: list[str] = []
    topic_failures: list[str] = []
    topic_audits: dict[str, dict] = {}

    print(f"Remediating {len(topics)} topic preview PDF(s)...")
    results, _stopped = run_topic_batch(
        s3, stepfunctions, a11y, sm_arn, args.course_id, topics, args
    )
    for result in results:
        if result.status == "failed":
            topic_failures.append(result.topic_id)
        elif result.audit:
            topic_audits[result.topic_id] = result.audit
        if result.preview_key and result.status == "remediated":
            paths_overwritten.append(result.preview_key)

    invalidation_id = None
    if not args.skip_cdn_invalidation and paths_overwritten:
        cdn_paths = [f"/{path}" for path in paths_overwritten]
        invalidation_id = invalidate_paths(distribution_id, cdn_paths)
        print(f"CloudFront invalidation: {invalidation_id or 'skipped'}")

    report = {
        "runId": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "scope": "chapter" if args.chapter_id else "course",
        "mode": "preview-only",
        "courseId": args.course_id,
        "tocId": args.toc_id,
        "chapterId": args.chapter_id,
        "env": args.env,
        "dryRun": False,
        "topics": {
            "expected": len(topics),
            "remediated": len(topics) - len(topic_failures),
            "failed": topic_failures,
            "audits": topic_audits,
        },
        "pathsOverwritten": paths_overwritten,
        "cdnInvalidation": {
            "distributionId": distribution_id,
            "invalidationId": invalidation_id,
        },
        "knownLimitations": [
            "Character encoding may remain failed on manual math PDFs (CID fonts, ADBE_IsScanned).",
        ],
    }

    reports_dir = Path(__file__).resolve().parents[1] / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / f"migrate-{report['runId']}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report: {report_path}")

    if topic_failures:
        print(
            f"Migration finished with {len(topic_failures)} topic failure(s): "
            f"{', '.join(topic_failures)}",
            flush=True,
        )
        print("Re-run to retry failed topics.", flush=True)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
