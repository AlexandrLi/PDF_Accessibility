"""Rebuild one course's downloads and chapter books on dev, then wait for them.

`generateCoursePdf` invokes one `generateTopicPdf` per topic and one
`generateChaptersPdf` per TOC with `InvocationType: Event`, so it returns as
soon as the requests are queued. A chapter lambda that runs out of clock throws
on purpose to earn Lambda's async retries, and a retry rebuilds the whole book,
so a successful invocation says nothing about whether any worksheet is current.
The only evidence is the S3 object, which is what `wait_for_worksheets` reads.

Every handler in generate-pdf-lambda answers 403 outside the dev stage, so this
is a dev-only step.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError


def course_pdf_function_name(env: str) -> str:
    return f"generate-pdf-{env}-generateCoursePdf"


def worksheet_key(course_id: str, chapter_title: str, toc_id: str) -> str:
    """The key the chapter lambda writes (generate-pdf-lambda worksheet-keys.ts)."""
    return f"courses/{course_id}/worksheets/{chapter_title}-{toc_id}.pdf"


def worksheet_keys(
    course: dict[str, Any],
    course_id: str,
    toc_id: str,
    *,
    has_preview: Callable[[str], bool] | None = None,
) -> dict[str, str]:
    """Chapter id to worksheet key, over the chapters the lambda actually writes.

    It skips a chapter whose every topic lacks a title or a PDF, so those are
    left out here too; waiting on a book nothing writes would only ever time out.
    The lambda also skips a chapter whose every preview is missing from S3, so
    pass `has_preview` to drop those as well.
    """
    toc = (course.get("tocs") or {}).get(toc_id) or {}
    topics = course.get("topics") or {}
    keys: dict[str, str] = {}
    for chapter in toc.get("chapters") or []:
        chapter_id = chapter.get("id")
        title = chapter.get("title")
        if not chapter_id or not title:
            continue
        usable = [
            topic_id
            for reference in chapter.get("topics") or []
            if (topic_id := (reference or {}).get("id"))
            and (topics.get(topic_id) or {}).get("title")
            and (topics.get(topic_id) or {}).get("pdfAvailable")
            and (has_preview is None or has_preview(topic_id))
        ]
        if not usable:
            continue
        keys[chapter_id] = worksheet_key(course_id, title, toc_id)
    return keys


def object_state(s3_client, bucket: str, key: str) -> dict[str, Any] | None:
    """The ETag and LastModified of one object, or None when it is absent."""
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return {
        "etag": str(head.get("ETag") or "").strip('"'),
        "lastModified": head.get("LastModified"),
    }


def snapshot_worksheets(
    s3_client, bucket: str, keys: dict[str, str]
) -> dict[str, dict[str, Any] | None]:
    """What each worksheet looks like before the rebuild."""
    return {
        chapter_id: object_state(s3_client, bucket, key)
        for chapter_id, key in keys.items()
    }


def _moved_on(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if before.get("etag") != after.get("etag"):
        return True
    first, second = before.get("lastModified"), after.get("lastModified")
    return bool(first and second and second > first)


def wait_for_worksheets(
    s3_client,
    bucket: str,
    keys: dict[str, str],
    baseline: dict[str, dict[str, Any] | None],
    *,
    timeout: float,
    poll: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_poll: Callable[[int, int, float], None] | None = None,
) -> dict[str, Any]:
    """Watch each worksheet until S3 shows a new object for it, or time runs out.

    A worksheet is rebuilt once it appears, its ETag changes, or its
    LastModified moves on. Both comparisons read S3's own clock, so the result
    does not depend on this machine's time, and the LastModified half catches a
    chapter that rebuilt to the same bytes.
    """
    started = clock()
    pending = dict(keys)
    rebuilt: dict[str, str] = {}
    # A throttle or a dropped connection during a wait this long must not end
    # the run: count the failed polls and read the key again on the next one.
    poll_errors = 0
    last_poll_error: str | None = None
    while True:
        for chapter_id, key in list(pending.items()):
            try:
                state = object_state(s3_client, bucket, key)
            except Exception as error:
                poll_errors += 1
                last_poll_error = f"{type(error).__name__}: {error}"
                continue
            if state is None:
                continue
            before = baseline.get(chapter_id)
            if before is None or _moved_on(before, state):
                rebuilt[chapter_id] = key
                pending.pop(chapter_id)
        elapsed = clock() - started
        if on_poll is not None:
            on_poll(len(rebuilt), len(keys), elapsed)
        if not pending or elapsed >= timeout:
            break
        sleep(min(poll, timeout - elapsed))
    result: dict[str, Any] = {
        "waitedSeconds": round(clock() - started, 1),
        "rebuilt": rebuilt,
        "pending": pending,
        "timedOut": bool(pending),
    }
    if poll_errors:
        result["pollErrors"] = poll_errors
        result["lastPollError"] = last_poll_error
    return result


def invoke_course_rebuild(
    lambda_client, course_id: str, *, function_name: str
) -> dict[str, Any]:
    """Call generateCoursePdf once and report what it said it dispatched."""
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps({"courseId": course_id}).encode("utf-8"),
    )
    raw = response.get("Payload")
    body = raw.read() if hasattr(raw, "read") else (raw or b"")
    result: dict[str, Any] = {
        "functionName": function_name,
        "statusCode": response.get("StatusCode"),
        "functionError": response.get("FunctionError"),
    }
    try:
        payload = json.loads(body.decode("utf-8") or "null")
    except ValueError:
        result["payload"] = body.decode("utf-8", "replace")[:2000]
        return result
    result["payload"] = payload
    if isinstance(payload, dict):
        result["handlerStatus"] = payload.get("statusCode")
        try:
            result["dispatched"] = json.loads(payload.get("body") or "null")
        except (ValueError, TypeError):
            result["dispatched"] = None
    return result


def rebuild_dispatched(invocation: dict[str, Any]) -> bool:
    """True when every chapter-book request reached Lambda.

    The handler answers 500 when any one fan-out request failed, and most of
    those requests are topic downloads. The chapter books are what the tracker's
    rows describe, so a failed topic dispatch is a warning (see
    `dispatch_warning`), not a reason to skip the wait.
    """
    if invocation.get("functionError"):
        return False
    if invocation.get("handlerStatus") == 200:
        return True
    dispatched = invocation.get("dispatched")
    if not isinstance(dispatched, dict):
        return False
    tocs = dispatched.get("tocs") or {}
    return bool(tocs.get("total")) and not tocs.get("failed")


def dispatch_warning(invocation: dict[str, Any]) -> str | None:
    """What the handler could not queue, when it queued every chapter book."""
    dispatched = invocation.get("dispatched")
    if not isinstance(dispatched, dict):
        return None
    topics = dispatched.get("topics") or {}
    failed = topics.get("failed") or 0
    if not failed:
        return None
    return (
        f"{failed} of {topics.get('total')} topic download requests failed to "
        "dispatch, so those wraps still predate this run"
    )
