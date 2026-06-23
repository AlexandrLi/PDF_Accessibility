#!/usr/bin/env python3
"""One-time Channels topic preview a11y migration.

Scope (v1): remediate topic_pdfs/{topicId}.pdf only.
Download and chapter worksheets are rebuilt separately via admin Generate worksheets.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from lib.channels_paths import (  # noqa: E402
    TopicScope,
    resolve_migration_scope,
)
from lib.cloudfront import invalidate_paths  # noqa: E402
from lib.config import (  # noqa: E402
    a11y_bucket,
    channels_bucket,
    cloudfront_distribution_id,
    state_machine_arn,
)
from lib.remediation import remediate_preview_pdf  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remediate Channels topic preview PDFs (topic_pdfs/{topicId}.pdf only)"
    )
    parser.add_argument("--course-id", required=True)
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    parser.add_argument("--toc-id", default=None)
    parser.add_argument(
        "--chapter-id",
        default=None,
        help="If set, only remediate preview PDFs for topics in this chapter",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-cdn-invalidation", action="store_true")
    return parser.parse_args()


def print_scope(topics: list[TopicScope], chapter_id: str | None) -> None:
    print(f"Topic previews in scope: {len(topics)}")
    if chapter_id:
        print(f"Chapter filter: {chapter_id}")
    for topic in topics:
        print(f"  - {topic.topic_id}: {topic.title}")
        print(f"      preview: {topic.preview_key}")


def main() -> int:
    args = parse_args()
    s3 = boto3.client("s3")
    stepfunctions = boto3.client("stepfunctions")

    channels = channels_bucket(args.env)
    a11y = a11y_bucket()
    sm_arn = state_machine_arn()
    distribution_id = cloudfront_distribution_id(args.env)

    topics, _chapters = resolve_migration_scope(
        s3,
        channels,
        args.course_id,
        chapter_id=args.chapter_id,
        toc_id=args.toc_id,
    )

    if not topics:
        print("Nothing to migrate for the requested scope.")
        return 1

    print_scope(topics, args.chapter_id)
    if args.dry_run:
        print("Dry run complete — no writes performed.")
        print("Next step after live run: admin Generate worksheets for download/chapter PDFs.")
        return 0

    paths_overwritten: list[str] = []
    topic_failures: list[str] = []

    print(f"Remediating {len(topics)} topic preview PDF(s)...")
    for topic in topics:
        print(f"  Adobe pass: {topic.topic_id} ({topic.title})")
        try:
            preview_bytes = s3.get_object(Bucket=channels, Key=topic.preview_key)["Body"].read()
            remediated = remediate_preview_pdf(
                s3,
                stepfunctions,
                a11y,
                sm_arn,
                preview_bytes,
                args.course_id,
                topic.topic_id,
                topic.title,
            )
            s3.put_object(
                Bucket=channels,
                Key=topic.preview_key,
                Body=remediated,
                ContentType="application/pdf",
            )
            paths_overwritten.append(topic.preview_key)
            print("    OK preview replaced")
        except Exception as error:
            print(f"    FAILED {topic.topic_id}: {error}")
            topic_failures.append(topic.topic_id)

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
        },
        "pathsOverwritten": paths_overwritten,
        "cdnInvalidation": {
            "distributionId": distribution_id,
            "invalidationId": invalidation_id,
        },
        "nextStep": "Run admin Generate worksheets to rebuild topic download and chapter PDFs",
    }

    reports_dir = Path(__file__).resolve().parents[1] / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / f"migrate-{report['runId']}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report: {report_path}")
    print("Next: run admin Generate worksheets for this course to refresh download/chapter PDFs.")

    if topic_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
