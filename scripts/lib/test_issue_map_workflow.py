"""Integration tests for compact, resumable issue-map preparation."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pikepdf

import sweep_accessibility_issue_map as workflow


def _pdf_bytes() -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page.obj["/Tabs"] = pikepdf.Name("/S")
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
    pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array(
                [
                    pikepdf.Dictionary(
                        {
                            "/Type": pikepdf.Name("/StructElem"),
                            "/S": pikepdf.Name("/Document"),
                        }
                    )
                ]
            ),
        }
    )
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


class FakeReadS3:
    def __init__(self, course: dict, pdf_bytes: bytes) -> None:
        self.course = course
        self.pdf_bytes = pdf_bytes
        self.pdf_downloads = 0

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        return {"ETag": '"source-etag"'}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        if Key.endswith("/course.json"):
            return {"Body": io.BytesIO(json.dumps(self.course).encode("utf-8"))}
        self.pdf_downloads += 1
        return {"Body": io.BytesIO(self.pdf_bytes)}


class IssueMapWorkflowTests(unittest.TestCase):
    def test_prepare_writes_compact_artifacts_and_resume_reuses_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            issue_map = root / "map.json"
            report = root / "report.json"
            manifest = root / "manifest.json"
            diagnostics = root / "diagnostics"
            review = root / "review.json"
            originals = root / "originals"
            swept = root / "reswept"
            issue_map.write_text(
                json.dumps(
                    {
                        "summary": {"topicRows": 1},
                        "topics": [
                            {
                                "sheet": "Course",
                                "row": 2,
                                "topicTitle": "Topic",
                                "chapterContext": {
                                    "id": "1",
                                    "title": "Chapter",
                                    "row": 1,
                                },
                                "failedColumns": ["G"],
                                "failedCategories": ["Tab Order"],
                                "failureCount": 1,
                            }
                        ],
                    }
                )
            )
            course = {
                "details": {"title": "Course", "defaultToc": "toc"},
                "tocs": {
                    "toc": {
                        "chapters": [
                            {
                                "id": "chapter",
                                "title": "1. Chapter",
                                "topics": [{"id": "topic"}],
                            }
                        ]
                    }
                },
                "topics": {
                    "topic": {
                        "title": "Topic",
                        "pdfAvailable": True,
                    }
                },
            }
            s3 = FakeReadS3(course, _pdf_bytes())
            args = argparse.Namespace(
                map=str(issue_map),
                course_id="course",
                map_sheet=None,
                env="dev",
                output_root=str(root / "unused"),
                original_dir=str(originals),
                swept_dir=str(swept),
                report=str(report),
                manifest=str(manifest),
                diagnostics_dir=str(diagnostics),
                review_queue=str(review),
                workers=2,
                resume=False,
                title_alias=[],
                all_topics=False,
            )

            with (
                patch.object(workflow, "parse_args", return_value=args),
                patch.object(workflow.boto3, "client", return_value=s3),
            ):
                self.assertEqual(workflow.main(), 0)

            compact = json.loads(manifest.read_text())
            self.assertEqual(compact["schemaVersion"], 2)
            self.assertEqual(compact["counts"]["resolved"], 1)
            self.assertEqual(compact["counts"]["downloaded"], 1)
            self.assertEqual(compact["counts"]["reused"], 0)
            self.assertEqual(len(compact["topics"]), 1)
            self.assertTrue(compact["topics"][0]["renderIdentical"])
            self.assertNotIn("repairs", compact["topics"][0])
            self.assertTrue((diagnostics / "topic.json").is_file())
            self.assertTrue(review.is_file())
            self.assertLess(manifest.stat().st_size, 5000)
            downloads_after_first = s3.pdf_downloads

            args.resume = True
            with (
                patch.object(workflow, "parse_args", return_value=args),
                patch.object(workflow.boto3, "client", return_value=s3),
            ):
                self.assertEqual(workflow.main(), 0)

            resumed = json.loads(manifest.read_text())
            self.assertEqual(resumed["counts"]["downloaded"], 0)
            self.assertEqual(resumed["counts"]["reused"], 1)
            self.assertEqual(s3.pdf_downloads, downloads_after_first)


if __name__ == "__main__":
    unittest.main()
