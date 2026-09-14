"""Tests for the chapter rebuild step of the unattended run."""

from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from lib.course_rebuild import (
    course_pdf_function_name,
    dispatch_warning,
    invoke_course_rebuild,
    rebuild_dispatched,
    snapshot_worksheets,
    wait_for_worksheets,
    worksheet_key,
    worksheet_keys,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

COURSE = {
    "topics": {
        "t1": {"title": "Cells", "pdfAvailable": True},
        "t2": {"title": "Enzymes", "pdfAvailable": False},
        "t3": {"pdfAvailable": True},
    },
    "tocs": {
        "toc1": {
            "chapters": [
                {"id": "c1", "title": "Chapter 1: Cells", "topics": [{"id": "t1"}]},
                {"id": "c2", "title": "Chapter 2: Enzymes", "topics": [{"id": "t2"}, {"id": "t3"}]},
                {"id": "c3", "title": "Chapter 3: Empty", "topics": []},
            ]
        }
    },
}


class FakeS3:
    """A head_object that answers from a dict of key to (etag, lastModified)."""

    def __init__(self, states: dict[str, tuple[str, datetime] | None]) -> None:
        self.states = states
        self.heads = 0

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.heads += 1
        state = self.states.get(Key)
        if state is None:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        etag, modified = state
        return {"ETag": f'"{etag}"', "LastModified": modified}


class KeyTests(unittest.TestCase):
    def test_worksheet_key_matches_the_lambda(self) -> None:
        self.assertEqual(
            worksheet_key("intro-bio", "Chapter 1: Cells", "toc1"),
            "courses/intro-bio/worksheets/Chapter 1: Cells-toc1.pdf",
        )

    def test_chapters_without_a_pdf_bearing_topic_are_left_out(self) -> None:
        self.assertEqual(
            worksheet_keys(COURSE, "intro-bio", "toc1"),
            {"c1": "courses/intro-bio/worksheets/Chapter 1: Cells-toc1.pdf"},
        )

    def test_a_chapter_whose_previews_are_missing_is_left_out(self) -> None:
        self.assertEqual(
            worksheet_keys(COURSE, "intro-bio", "toc1", has_preview=lambda _id: False),
            {},
        )

    def test_function_name_follows_the_stage(self) -> None:
        self.assertEqual(course_pdf_function_name("dev"), "generate-pdf-dev-generateCoursePdf")


class WaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keys = {"c1": "a.pdf", "c2": "b.pdf"}

    def test_a_changed_etag_or_a_new_object_counts_as_rebuilt(self) -> None:
        s3 = FakeS3({"a.pdf": ("old", NOW)})
        baseline = snapshot_worksheets(s3, "bucket", self.keys)
        self.assertIsNone(baseline["c2"])
        s3.states["a.pdf"] = ("new", NOW)
        s3.states["b.pdf"] = ("fresh", NOW)
        result = wait_for_worksheets(
            s3, "bucket", self.keys, baseline, timeout=300, poll=60, sleep=lambda _s: None
        )
        self.assertEqual(result["rebuilt"], self.keys)
        self.assertFalse(result["timedOut"])

    def test_same_bytes_written_later_counts_as_rebuilt(self) -> None:
        s3 = FakeS3({"a.pdf": ("same", NOW)})
        baseline = snapshot_worksheets(s3, "bucket", {"c1": "a.pdf"})
        s3.states["a.pdf"] = ("same", NOW + timedelta(minutes=5))
        result = wait_for_worksheets(
            s3, "bucket", {"c1": "a.pdf"}, baseline, timeout=300, poll=60, sleep=lambda _s: None
        )
        self.assertEqual(result["rebuilt"], {"c1": "a.pdf"})

    def test_an_untouched_book_is_still_pending_when_the_wait_times_out(self) -> None:
        s3 = FakeS3({"a.pdf": ("old", NOW), "b.pdf": ("old", NOW)})
        baseline = snapshot_worksheets(s3, "bucket", self.keys)
        s3.states["a.pdf"] = ("new", NOW)
        elapsed = [0.0]

        def sleep(seconds: float) -> None:
            elapsed[0] += seconds

        result = wait_for_worksheets(
            s3,
            "bucket",
            self.keys,
            baseline,
            timeout=120,
            poll=60,
            sleep=sleep,
            clock=lambda: elapsed[0],
        )
        self.assertEqual(result["rebuilt"], {"c1": "a.pdf"})
        self.assertEqual(result["pending"], {"c2": "b.pdf"})
        self.assertTrue(result["timedOut"])


    def test_a_throttled_poll_is_retried_rather_than_raised(self) -> None:
        s3 = FakeS3({"a.pdf": ("old", NOW)})
        baseline = snapshot_worksheets(s3, "bucket", {"c1": "a.pdf"})
        s3.states["a.pdf"] = ("new", NOW)
        original = s3.head_object

        def throttle_once(*, Bucket: str, Key: str) -> dict[str, object]:
            s3.head_object = original
            raise ClientError({"Error": {"Code": "SlowDown"}}, "HeadObject")

        s3.head_object = throttle_once
        result = wait_for_worksheets(
            s3, "bucket", {"c1": "a.pdf"}, baseline, timeout=300, poll=60, sleep=lambda _s: None
        )
        self.assertEqual(result["rebuilt"], {"c1": "a.pdf"})
        self.assertEqual(result["pollErrors"], 1)


class FakeLambda:
    def __init__(self, payload: object, **response: object) -> None:
        self.payload = payload
        self.response = response
        self.calls: list[dict[str, object]] = []

    def invoke(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        body = json.dumps(self.payload).encode("utf-8")
        return {"StatusCode": 200, "Payload": io.BytesIO(body), **self.response}


class InvokeTests(unittest.TestCase):
    def test_the_handler_body_is_reported_as_dispatched(self) -> None:
        client = FakeLambda({"statusCode": 200, "body": json.dumps({"topics": 3, "tocs": 1})})
        result = invoke_course_rebuild(client, "intro-bio", function_name="fn")
        self.assertEqual(
            json.loads(client.calls[0]["Payload"].decode("utf-8")), {"courseId": "intro-bio"}
        )
        self.assertEqual(client.calls[0]["InvocationType"], "RequestResponse")
        self.assertEqual(result["dispatched"], {"topics": 3, "tocs": 1})
        self.assertTrue(rebuild_dispatched(result))

    def test_a_handler_error_or_a_failed_book_is_not_dispatched(self) -> None:
        client = FakeLambda({"statusCode": 500, "body": "{}"})
        self.assertFalse(rebuild_dispatched(invoke_course_rebuild(client, "c", function_name="fn")))
        books = FakeLambda(
            {
                "statusCode": 500,
                "body": json.dumps({"topics": {"total": 3, "failed": 0}, "tocs": {"total": 2, "failed": 1}}),
            }
        )
        self.assertFalse(rebuild_dispatched(invoke_course_rebuild(books, "c", function_name="fn")))
        raised = FakeLambda({"errorMessage": "boom"}, FunctionError="Unhandled")
        self.assertFalse(rebuild_dispatched(invoke_course_rebuild(raised, "c", function_name="fn")))

    def test_a_failed_topic_dispatch_still_waits_and_is_warned_about(self) -> None:
        client = FakeLambda(
            {
                "statusCode": 500,
                "body": json.dumps({"topics": {"total": 72, "failed": 1}, "tocs": {"total": 8, "failed": 0}}),
            }
        )
        result = invoke_course_rebuild(client, "intro-bio", function_name="fn")
        self.assertTrue(rebuild_dispatched(result))
        self.assertEqual(
            dispatch_warning(result),
            "1 of 72 topic download requests failed to dispatch, so those wraps "
            "still predate this run",
        )
        self.assertIsNone(dispatch_warning({"dispatched": {"topics": {"total": 3, "failed": 0}}}))


if __name__ == "__main__":
    unittest.main()
