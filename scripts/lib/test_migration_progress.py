"""Tests for S3 migration progress helpers."""

from __future__ import annotations

import unittest

from lib.migration_progress import mark_chapter_completed, pending_failed_topic_ids


class MarkChapterCompletedTests(unittest.TestCase):
    def test_advances_next_index_when_frontier_chapter_completes(self) -> None:
        progress = {"nextChapterIndex": 2, "completedChapters": []}

        mark_chapter_completed(progress, "ch-02", 2)

        self.assertEqual(progress["nextChapterIndex"], 3)
        self.assertEqual(progress["completedChapters"], ["ch-02"])

    def test_does_not_skip_past_earlier_incomplete_chapter(self) -> None:
        progress = {"nextChapterIndex": 3, "completedChapters": []}

        mark_chapter_completed(progress, "ch-05", 5)

        self.assertEqual(progress["nextChapterIndex"], 3)
        self.assertEqual(progress["completedChapters"], ["ch-05"])


class PendingFailedTopicIdsTests(unittest.TestCase):
    def test_returns_failed_topics_not_yet_completed(self) -> None:
        progress = {
            "completedTopics": ["done-1"],
            "failedTopics": ["done-1", "still-failed", "pending-retry"],
        }

        self.assertEqual(
            pending_failed_topic_ids(progress),
            ["still-failed", "pending-retry"],
        )

    def test_returns_empty_when_no_failures(self) -> None:
        progress = {"completedTopics": ["done-1"], "failedTopics": []}

        self.assertEqual(pending_failed_topic_ids(progress), [])


if __name__ == "__main__":
    unittest.main()
