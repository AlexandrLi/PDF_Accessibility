import json
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError


@dataclass
class TopicScope:
    topic_id: str
    title: str
    preview_key: str


def course_json_key(course_id: str) -> str:
    return f"courses/{course_id}/{course_id}.json"


def preview_key(course_id: str, topic_id: str) -> str:
    return f"courses/{course_id}/topic_pdfs/{topic_id}.pdf"


def load_course_json(s3_client, bucket: str, course_id: str) -> dict:
    key = course_json_key(course_id)
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def find_chapter(course: dict, chapter_id: str, toc_id: str | None = None) -> tuple[str, dict]:
    tocs = course.get("tocs") or {}
    if toc_id:
        toc = tocs.get(toc_id)
        if not toc:
            raise ValueError(f"TOC not found: {toc_id}")
        for chapter in toc.get("chapters") or []:
            if chapter.get("id") == chapter_id:
                return toc_id, chapter
        raise ValueError(f"Chapter {chapter_id} not found in toc {toc_id}")

    for candidate_toc_id, toc in tocs.items():
        for chapter in toc.get("chapters") or []:
            if chapter.get("id") == chapter_id:
                return candidate_toc_id, chapter
    raise ValueError(f"Chapter {chapter_id} not found in any TOC")


def object_exists(s3_client, bucket: str, key: str) -> bool:
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def build_topic_scope(
    s3_client,
    bucket: str,
    course_id: str,
    course: dict,
    topic_id: str,
) -> TopicScope | None:
    topic = (course.get("topics") or {}).get(topic_id)
    if not topic:
        return None
    if not topic.get("pdfAvailable"):
        return None
    title = topic.get("title") or topic_id
    preview = preview_key(course_id, topic_id)
    if not object_exists(s3_client, bucket, preview):
        return None
    return TopicScope(
        topic_id=topic_id,
        title=title,
        preview_key=preview,
    )


def topics_for_chapter(
    s3_client,
    bucket: str,
    course_id: str,
    course: dict,
    chapter: dict,
) -> list[TopicScope]:
    scopes: list[TopicScope] = []
    for topic_ref in chapter.get("topics") or []:
        topic_id = topic_ref.get("id")
        if not topic_id:
            continue
        scope = build_topic_scope(s3_client, bucket, course_id, course, topic_id)
        if scope:
            scopes.append(scope)
    return scopes


def resolve_migration_scope(
    s3_client,
    bucket: str,
    course_id: str,
    chapter_id: str | None = None,
    toc_id: str | None = None,
) -> list[TopicScope]:
    course = load_course_json(s3_client, bucket, course_id)

    if chapter_id:
        _resolved_toc_id, chapter = find_chapter(course, chapter_id, toc_id)
        return topics_for_chapter(s3_client, bucket, course_id, course, chapter)

    topics: list[TopicScope] = []
    seen: set[str] = set()
    for topic_id in (course.get("topics") or {}):
        scope = build_topic_scope(s3_client, bucket, course_id, course, topic_id)
        if scope and topic_id not in seen:
            topics.append(scope)
            seen.add(topic_id)
    return topics
