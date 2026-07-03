#!/usr/bin/env python3
"""One-time Channels topic preview a11y migration.

Remediates topic_pdfs/{topicId}.pdf via the Adobe accessibility pipeline.
Topic download and chapter worksheets are rebuilt separately via generate-pdf-lambda.
"""

from __future__ import annotations

import argparse
import json
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
from lib.layout_table_sweep import repair_layout_tables  # noqa: E402
from lib.migration_progress import (  # noqa: E402
    clear_topic_failed,
    completed_topic_ids,
    load_progress,
    mark_chapter_completed,
    mark_topic_completed,
    mark_topic_failed,
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
        help="Write preview even when figures lack /Alt (reported in migration JSON)",
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
    return parser.parse_args()


def print_scope(topics: list[TopicScope], chapter_id: str | None) -> None:
    print(f"Topic previews in scope: {len(topics)}")
    if chapter_id:
        print(f"Chapter filter: {chapter_id}")
    for topic in topics:
        print(f"  - {topic.topic_id}: {topic.title}")
        print(f"      preview: {topic.preview_key}")


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

        if args.skip_if_audited and is_likely_remediated(preview_bytes):
            audit = audit_pdf_bytes(preview_bytes)
            print("    SKIP already remediated (local audit)")
            return TopicRunResult(
                topic_id=topic.topic_id,
                status="skipped-audited",
                audit=audit.to_dict(),
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
        audit_before = audit_pdf_bytes(remediated)
        repaired_figures: list[int] = []
        if audit_before.figures_missing_alt and not args.no_repair_missing_figure_alt:
            remediated, repaired_figures = repair_missing_figure_alt(remediated)
            if repaired_figures:
                print(f"    repaired figure alt: {repaired_figures}")

        table_repair = None
        if not args.no_repair_layout_tables:
            remediated, table_repair = repair_layout_tables(remediated)
            if table_repair.actions:
                for action in table_repair.actions:
                    print(f"    {action}")

        audit = audit_pdf_bytes(remediated)
        audit_payload = {
            **audit.to_dict(),
            "beforeRepair": audit_before.to_dict(),
            "repairedFigures": repaired_figures,
            "tableRepair": table_repair.to_dict() if table_repair else None,
        }
        print(
            f"    audit: {audit.figure_count} figures, "
            f"{len(audit.figures_missing_alt)} missing alt, "
            f"{audit.table_count} tables"
        )
        if audit.has_blocking_issues and not args.allow_missing_figure_alt:
            raise RuntimeError(f"figures missing /Alt: {audit.figures_missing_alt}")

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

    if args.dry_run:
        print_auto_chapter_plan(s3, channels, chapters, reference_toc_id, progress, args)
        print("Dry run complete — no writes performed.")
        return 0

    deadline = time.monotonic() + args.time_budget_seconds
    paths_overwritten: list[str] = []
    topic_results: dict[str, dict] = {}
    topic_failures: list[str] = []
    stopped_early = False
    chapters_processed = 0

    start_index = int(progress.get("nextChapterIndex") or 0)
    print(
        f"Auto-chapters: {len(chapters)} chapters, starting at index {start_index}, "
        f"time budget {args.time_budget_seconds}s"
    )

    for index in range(start_index, len(chapters)):
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
            f"({len(pending_topics)} topic(s))"
        )
        chapter_failures: list[str] = []
        for topic in pending_topics:
            if time.monotonic() >= deadline:
                print("Time budget reached mid-chapter.")
                progress["nextChapterIndex"] = index
                stopped_early = True
                break

            print(f"  Adobe pass: {topic.topic_id} ({topic.title})")
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
                continue

            clear_topic_failed(progress, topic.topic_id)

            skipped_reason = "skip-if-audited" if result.status == "skipped-audited" else None
            mark_topic_completed(progress, topic.topic_id, skipped_reason=skipped_reason)
            done.add(topic.topic_id)
            if result.preview_key and result.status == "remediated":
                paths_overwritten.append(result.preview_key)

        if stopped_early:
            save_progress(s3, channels, args.course_id, progress, run_id)
            break

        if chapter_failures:
            print(f"Chapter {chapter.chapter_id} had failures — not marking chapter complete")
            progress["nextChapterIndex"] = index
            save_progress(s3, channels, args.course_id, progress, run_id)
            break

        mark_chapter_completed(progress, chapter.chapter_id, index)
        chapters_processed += 1
        save_progress(s3, channels, args.course_id, progress, run_id)
        print(f"Chapter [{index}] complete — progress saved")

    chapter_walk_complete = int(progress.get("nextChapterIndex") or 0) >= len(chapters)
    if not stopped_early and chapter_walk_complete:
        course_topics = resolve_migration_scope(s3, channels, args.course_id)
        toc_topic_ids = {topic.topic_id for chapter in chapters for topic in chapter.topics}
        orphan_topics = [
            topic
            for topic in course_topics
            if topic.topic_id not in toc_topic_ids and topic.topic_id not in done
        ]
        if orphan_topics:
            print(f"Orphan pass: {len(orphan_topics)} topic(s) outside reference TOC chapters")
            for topic in orphan_topics:
                if time.monotonic() >= deadline:
                    stopped_early = True
                    save_progress(s3, channels, args.course_id, progress, run_id)
                    break
                print(f"  Adobe pass: {topic.topic_id} ({topic.title})")
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
                    continue
                clear_topic_failed(progress, topic.topic_id)
                skipped_reason = "skip-if-audited" if result.status == "skipped-audited" else None
                mark_topic_completed(progress, topic.topic_id, skipped_reason=skipped_reason)
                done.add(topic.topic_id)
                if result.preview_key and result.status == "remediated":
                    paths_overwritten.append(result.preview_key)
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
        return 1
    print("Auto-chapters migration complete for course scope.")
    return 0


def main() -> int:
    args = parse_args()
    if args.auto_chapters and args.chapter_id:
        print("Use either --auto-chapters or --chapter-id, not both.")
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
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
