"""S3-backed progress tracking for course-wide preview migration."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

PROGRESS_VERSION = 1


def progress_key(course_id: str) -> str:
    return f"courses/{course_id}/.a11y-migration-progress.json"


def empty_progress(course_id: str, reference_toc_id: str, env: str) -> dict[str, Any]:
    return {
        "version": PROGRESS_VERSION,
        "courseId": course_id,
        "referenceTocId": reference_toc_id,
        "env": env,
        "completedChapters": [],
        "completedTopics": [],
        "skippedTopics": {},
        "failedTopics": [],
        "nextChapterIndex": 0,
        "lastRunId": None,
        "updatedAt": None,
    }


def load_progress(
    s3_client,
    bucket: str,
    course_id: str,
    reference_toc_id: str,
    env: str,
) -> dict[str, Any]:
    key = progress_key(course_id)
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        data = json.loads(response["Body"].read().decode("utf-8"))
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return empty_progress(course_id, reference_toc_id, env)
        raise

    if data.get("version") != PROGRESS_VERSION:
        data["version"] = PROGRESS_VERSION
    data.setdefault("completedChapters", [])
    data.setdefault("completedTopics", [])
    data.setdefault("skippedTopics", {})
    data.setdefault("failedTopics", [])
    data.setdefault("nextChapterIndex", 0)
    data["courseId"] = course_id
    data["referenceTocId"] = reference_toc_id
    data["env"] = env
    return data


def save_progress(
    s3_client,
    bucket: str,
    course_id: str,
    progress: dict[str, Any],
    run_id: str | None = None,
) -> str:
    progress["updatedAt"] = datetime.now(timezone.utc).isoformat()
    if run_id:
        progress["lastRunId"] = run_id
    key = progress_key(course_id)
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(progress, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def completed_topic_ids(progress: dict[str, Any]) -> set[str]:
    return set(progress.get("completedTopics") or [])


def mark_topic_completed(
    progress: dict[str, Any],
    topic_id: str,
    *,
    skipped_reason: str | None = None,
) -> None:
    completed = progress.setdefault("completedTopics", [])
    if topic_id not in completed:
        completed.append(topic_id)
    if skipped_reason:
        progress.setdefault("skippedTopics", {})[topic_id] = skipped_reason


def mark_topic_failed(progress: dict[str, Any], topic_id: str) -> None:
    failed = progress.setdefault("failedTopics", [])
    if topic_id not in failed:
        failed.append(topic_id)


def clear_topic_failed(progress: dict[str, Any], topic_id: str) -> None:
    failed = progress.get("failedTopics") or []
    if topic_id in failed:
        progress["failedTopics"] = [item for item in failed if item != topic_id]


def mark_chapter_completed(progress: dict[str, Any], chapter_id: str, chapter_index: int) -> None:
    completed = progress.setdefault("completedChapters", [])
    if chapter_id not in completed:
        completed.append(chapter_id)
    progress["nextChapterIndex"] = chapter_index + 1


def migration_status_summary(
    progress: dict[str, Any],
    chapters: list,
) -> dict[str, Any]:
    completed_chapters = set(progress.get("completedChapters") or [])
    completed_topics = completed_topic_ids(progress)
    next_index = int(progress.get("nextChapterIndex") or 0)
    next_chapter = None
    if 0 <= next_index < len(chapters):
        chapter = chapters[next_index]
        next_chapter = {
            "index": next_index,
            "chapterId": chapter.chapter_id,
            "title": chapter.title,
            "topicCount": len(chapter.topics),
        }

    total_topics = sum(len(chapter.topics) for chapter in chapters)
    return {
        "referenceTocId": progress.get("referenceTocId"),
        "chaptersTotal": len(chapters),
        "chaptersCompleted": len(completed_chapters),
        "topicsTotalInToc": total_topics,
        "topicsCompleted": len(completed_topics),
        "topicsFailed": len(progress.get("failedTopics") or []),
        "nextChapter": next_chapter,
        "lastRunId": progress.get("lastRunId"),
        "updatedAt": progress.get("updatedAt"),
    }
