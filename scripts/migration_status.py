#!/usr/bin/env python3
"""Print S3-backed migration progress for a course."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from lib.channels_paths import resolve_auto_chapter_plan  # noqa: E402
from lib.config import channels_bucket  # noqa: E402
from lib.migration_progress import load_progress, migration_status_summary  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show preview migration progress for a course")
    parser.add_argument("--course-id", required=True)
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    parser.add_argument("--toc-id", default=None)
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    s3 = boto3.client("s3")
    channels = channels_bucket(args.env)
    reference_toc_id, chapters = resolve_auto_chapter_plan(
        s3, channels, args.course_id, args.toc_id
    )
    progress = load_progress(s3, channels, args.course_id, reference_toc_id, args.env)
    summary = migration_status_summary(progress, chapters)
    summary["courseId"] = args.course_id
    summary["env"] = args.env
    summary["progressKey"] = f"courses/{args.course_id}/.a11y-migration-progress.json"

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"Course: {args.course_id} ({args.env})")
    print(f"Progress file: {summary['progressKey']}")
    print(f"Reference TOC: {summary['referenceTocId']}")
    print(
        f"Chapters: {summary['chaptersCompleted']}/{summary['chaptersTotal']} complete"
    )
    print(
        f"Topics: {summary['topicsCompleted']}/{summary['topicsTotalInToc']} complete "
        f"({summary['topicsFailed']} failed)"
    )
    if summary["lastRunId"]:
        print(f"Last run: {summary['lastRunId']} (updated {summary['updatedAt']})")
    next_chapter = summary.get("nextChapter")
    if next_chapter:
        print(
            f"Next chapter [{next_chapter['index']}]: "
            f"{next_chapter['title']} ({next_chapter['topicCount']} topics) "
            f"id={next_chapter['chapterId']}"
        )
    elif summary["chaptersCompleted"] >= summary["chaptersTotal"]:
        print("Next chapter: none — TOC chapter walk complete (check orphan pass on next run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
