"""Tests for fail-closed manifest-driven S3 PDF replacement."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import pikepdf

from lib.s3_pdf_replacement import (
    ReplacementError,
    manifest_sha256,
    publish_manifest,
    rollback_report,
)


def _pdf_bytes(label: str) -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.docinfo["/Title"] = pikepdf.String(label)
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


class FakeS3:
    def __init__(self, *, versioning: str = "Suspended") -> None:
        self.versioning = versioning
        self.objects: dict[str, dict] = {}
        self.put_count = 0
        self.copy_count = 0
        self.fail_copy = False
        self.corrupt_put = False

    def add(self, key: str, body: bytes) -> None:
        self.objects[key] = {
            "body": body,
            "properties": {
                "ContentType": "application/pdf",
                "CacheControl": "max-age=60",
                "Metadata": {"source": "test"},
                "ServerSideEncryption": "AES256",
            },
            "tags": [{"Key": "kind", "Value": "preview"}],
            "acl": {
                "Owner": {"ID": "owner"},
                "Grants": [
                    {
                        "Grantee": {"Type": "CanonicalUser", "ID": "owner"},
                        "Permission": "FULL_CONTROL",
                    }
                ],
            },
            "versionId": "v1" if self.versioning == "Enabled" else "null",
        }

    def head_object(
        self,
        *,
        Bucket: str,
        Key: str,
        VersionId: str | None = None,
    ) -> dict:
        value = self.objects[Key]
        return {
            "ETag": f'"{hashlib.md5(value["body"]).hexdigest()}"',
            "ContentLength": len(value["body"]),
            "VersionId": value["versionId"],
            **copy.deepcopy(value["properties"]),
        }

    def get_object(
        self,
        *,
        Bucket: str,
        Key: str,
        VersionId: str | None = None,
    ) -> dict:
        return {"Body": io.BytesIO(self.objects[Key]["body"])}

    def get_object_tagging(
        self,
        *,
        Bucket: str,
        Key: str,
        VersionId: str | None = None,
    ) -> dict:
        return {"TagSet": copy.deepcopy(self.objects[Key]["tags"])}

    def get_object_acl(
        self,
        *,
        Bucket: str,
        Key: str,
        VersionId: str | None = None,
    ) -> dict:
        return copy.deepcopy(self.objects[Key]["acl"])

    def get_bucket_versioning(self, *, Bucket: str) -> dict:
        return {"Status": self.versioning}

    def copy_object(
        self,
        *,
        Bucket: str,
        Key: str,
        CopySource: dict,
        MetadataDirective: str,
        TaggingDirective: str,
    ) -> dict:
        if self.fail_copy:
            raise RuntimeError("copy failed")
        self.copy_count += 1
        self.objects[Key] = copy.deepcopy(self.objects[CopySource["Key"]])
        return {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs) -> dict:
        self.put_count += 1
        self.objects[Key] = {
            "body": _pdf_bytes("corrupt") if self.corrupt_put else Body,
            "properties": copy.deepcopy(kwargs),
            "tags": [],
            "acl": {"Owner": None, "Grants": []},
            "versionId": "v2" if self.versioning == "Enabled" else "null",
        }
        return {}

    def put_object_tagging(self, *, Bucket: str, Key: str, Tagging: dict) -> dict:
        self.objects[Key]["tags"] = copy.deepcopy(Tagging["TagSet"])
        return {}

    def put_object_acl(
        self,
        *,
        Bucket: str,
        Key: str,
        AccessControlPolicy: dict,
    ) -> dict:
        self.objects[Key]["acl"] = copy.deepcopy(AccessControlPolicy)
        return {}


class S3PdfReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = _pdf_bytes("original")
        self.fixed = _pdf_bytes("fixed")
        original_path = self.root / "original.pdf"
        fixed_path = self.root / "fixed.pdf"
        original_path.write_bytes(self.original)
        fixed_path.write_bytes(self.fixed)
        self.key = "courses/course/topic_pdfs/topic.pdf"
        self.manifest_path = self.root / "manifest.json"
        self.report_path = self.root / "report.json"
        self.manifest = {
            "schemaVersion": 2,
            "course": {
                "courseId": "course",
                "environment": "dev",
                "bucket": "channels-data-dev",
            },
            "topics": [
                {
                    "topicId": "topic",
                    "topicTitle": "Topic",
                    "pdfKey": self.key,
                    "originalPdf": "original.pdf",
                    "reSweptPdf": "fixed.pdf",
                    "originalSha256": hashlib.sha256(self.original).hexdigest(),
                    "reSweptSha256": hashlib.sha256(self.fixed).hexdigest(),
                    "resultKind": "resolved",
                    "renderIdentical": True,
                    "secondPassByteStable": True,
                }
            ],
        }
        self._write_manifest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest))

    def _s3(self, *, versioning: str = "Suspended") -> FakeS3:
        s3 = FakeS3(versioning=versioning)
        s3.add(self.key, self.original)
        return s3

    def test_plan_is_read_only_and_reports_manifest_digest(self) -> None:
        s3 = self._s3()

        report = publish_manifest(
            self.manifest_path,
            self.report_path,
            self.root,
            s3,
            apply=False,
        )

        self.assertEqual(report["outcome"], "planned")
        self.assertEqual(report["manifestSha256"], manifest_sha256(self.manifest_path))
        self.assertEqual(report["summary"]["planned"], 1)
        self.assertEqual(s3.put_count, 0)
        self.assertEqual(s3.copy_count, 0)

    def test_apply_requires_the_exact_approved_manifest_digest(self) -> None:
        with self.assertRaisesRegex(ReplacementError, "does not match"):
            publish_manifest(
                self.manifest_path,
                self.report_path,
                self.root,
                self._s3(),
                apply=True,
                approved_manifest_sha256="wrong",
            )

    def test_source_drift_fails_before_any_write(self) -> None:
        s3 = self._s3()
        s3.objects[self.key]["body"] = _pdf_bytes("drift")

        with self.assertRaisesRegex(ReplacementError, "source drift"):
            publish_manifest(
                self.manifest_path,
                self.report_path,
                self.root,
                s3,
                apply=False,
            )

        self.assertEqual(s3.put_count, 0)
        self.assertEqual(s3.copy_count, 0)

    def test_backup_barrier_prevents_replacement(self) -> None:
        s3 = self._s3()
        s3.fail_copy = True

        with self.assertRaisesRegex(RuntimeError, "copy failed"):
            publish_manifest(
                self.manifest_path,
                self.report_path,
                self.root,
                s3,
                apply=True,
                approved_manifest_sha256=manifest_sha256(self.manifest_path),
            )

        self.assertEqual(s3.put_count, 0)

    def test_apply_backs_up_preserves_verifies_and_invalidates(self) -> None:
        s3 = self._s3()
        invalidations: list[list[str]] = []

        report = publish_manifest(
            self.manifest_path,
            self.report_path,
            self.root,
            s3,
            apply=True,
            approved_manifest_sha256=manifest_sha256(self.manifest_path),
            invalidate=lambda paths: invalidations.append(paths) or "invalidation",
        )

        self.assertEqual(report["outcome"], "completed")
        self.assertEqual(report["summary"]["backedUp"], 1)
        self.assertEqual(report["summary"]["uploaded"], 1)
        self.assertEqual(report["summary"]["verified"], 1)
        self.assertEqual(report["summary"]["recoverable"], 1)
        self.assertEqual(s3.objects[self.key]["body"], self.fixed)
        self.assertTrue(report["objects"][0]["backupVerified"])
        self.assertTrue(report["objects"][0]["backupPropertiesPreserved"])
        self.assertTrue(report["objects"][0]["backupTagsPreserved"])
        self.assertTrue(report["objects"][0]["backupAclPreserved"])
        self.assertTrue(report["objects"][0]["propertiesPreserved"])
        self.assertTrue(report["objects"][0]["tagsPreserved"])
        self.assertTrue(report["objects"][0]["aclPreserved"])
        self.assertEqual(invalidations, [[f"/{self.key}"]])

    def test_already_applied_is_idempotent(self) -> None:
        s3 = self._s3()
        s3.objects[self.key]["body"] = self.fixed

        report = publish_manifest(
            self.manifest_path,
            self.report_path,
            self.root,
            s3,
            apply=True,
            approved_manifest_sha256=manifest_sha256(self.manifest_path),
        )

        self.assertEqual(report["summary"]["alreadyApplied"], 1)
        self.assertEqual(s3.put_count, 0)
        self.assertEqual(s3.copy_count, 0)

    def test_invalidation_failure_is_reported_and_retryable(self) -> None:
        s3 = self._s3()

        with self.assertRaisesRegex(ReplacementError, "invalidation failed"):
            publish_manifest(
                self.manifest_path,
                self.report_path,
                self.root,
                s3,
                apply=True,
                approved_manifest_sha256=manifest_sha256(self.manifest_path),
                invalidate=lambda _paths: (_ for _ in ()).throw(
                    RuntimeError("invalidation unavailable")
                ),
            )

        failed = json.loads(self.report_path.read_text())
        self.assertEqual(failed["outcome"], "invalidation-failed")
        self.assertEqual(s3.objects[self.key]["body"], self.fixed)
        invalidations: list[list[str]] = []
        retried = publish_manifest(
            self.manifest_path,
            self.report_path,
            self.root,
            s3,
            apply=True,
            approved_manifest_sha256=manifest_sha256(self.manifest_path),
            invalidate=lambda paths: invalidations.append(paths) or "retry",
        )
        self.assertEqual(retried["summary"]["alreadyApplied"], 1)
        self.assertEqual(invalidations, [[f"/{self.key}"]])

    def test_enabled_versioning_uses_original_version_without_backup_copy(self) -> None:
        s3 = self._s3(versioning="Enabled")

        report = publish_manifest(
            self.manifest_path,
            self.report_path,
            self.root,
            s3,
            apply=True,
            approved_manifest_sha256=manifest_sha256(self.manifest_path),
        )

        self.assertEqual(report["summary"]["recoverable"], 1)
        self.assertEqual(report["summary"]["backedUp"], 0)
        self.assertEqual(s3.copy_count, 0)
        self.assertEqual(s3.objects[self.key]["body"], self.fixed)

    def test_failed_replacement_readback_rolls_back_current_object(self) -> None:
        s3 = self._s3()
        s3.corrupt_put = True

        with self.assertRaisesRegex(ReplacementError, "read-back hash mismatch"):
            publish_manifest(
                self.manifest_path,
                self.report_path,
                self.root,
                s3,
                apply=True,
                approved_manifest_sha256=manifest_sha256(self.manifest_path),
            )

        self.assertEqual(s3.objects[self.key]["body"], self.original)
        report = json.loads(self.report_path.read_text())
        self.assertEqual(report["objects"][0]["status"], "rolled-back")
        self.assertEqual(report["summary"]["uploaded"], 0)

    def test_unresolved_topic_is_rejected_before_s3_access(self) -> None:
        self.manifest["topics"][0]["resultKind"] = "residual"
        self._write_manifest()
        s3 = self._s3()

        with self.assertRaisesRegex(ReplacementError, "requires resolved"):
            publish_manifest(
                self.manifest_path,
                self.report_path,
                self.root,
                s3,
                apply=False,
            )

        self.assertEqual(s3.put_count, 0)
        self.assertEqual(s3.copy_count, 0)

    def test_exact_topic_exception_allows_manual_adobe_approval(self) -> None:
        self.manifest["topics"][0]["resultKind"] = "unverifiable"
        self._write_manifest()

        report = publish_manifest(
            self.manifest_path,
            self.report_path,
            self.root,
            self._s3(),
            apply=False,
            approved_exceptions={"topic"},
        )

        self.assertEqual(report["approvedExceptions"], ["topic"])
        self.assertEqual(report["summary"]["planned"], 1)

    def test_render_changed_topic_is_rejected_without_exception(self) -> None:
        self.manifest["topics"][0]["renderIdentical"] = False
        self._write_manifest()
        s3 = self._s3()

        with self.assertRaisesRegex(ReplacementError, "rendering was not verified"):
            publish_manifest(
                self.manifest_path,
                self.report_path,
                self.root,
                s3,
                apply=False,
            )

        self.assertEqual(s3.put_count, 0)
        self.assertEqual(s3.copy_count, 0)

    def test_exact_render_exception_allows_visually_reviewed_topic(self) -> None:
        self.manifest["topics"][0]["renderIdentical"] = False
        self._write_manifest()

        report = publish_manifest(
            self.manifest_path,
            self.report_path,
            self.root,
            self._s3(),
            apply=False,
            approved_render_exceptions={"topic"},
        )

        self.assertEqual(report["approvedRenderExceptions"], ["topic"])
        self.assertEqual(report["summary"]["planned"], 1)

    def test_unknown_render_exception_topic_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ReplacementError, "render exception topic IDs"
        ):
            publish_manifest(
                self.manifest_path,
                self.report_path,
                self.root,
                self._s3(),
                apply=False,
                approved_render_exceptions={"missing-topic"},
            )

    def test_render_exception_does_not_bypass_resolved_status(self) -> None:
        self.manifest["topics"][0]["resultKind"] = "residual"
        self.manifest["topics"][0]["renderIdentical"] = False
        self._write_manifest()

        with self.assertRaisesRegex(ReplacementError, "requires resolved"):
            publish_manifest(
                self.manifest_path,
                self.report_path,
                self.root,
                self._s3(),
                apply=False,
                approved_render_exceptions={"topic"},
            )

    def test_legacy_detailed_manifest_is_normalized_safely(self) -> None:
        self.manifest = {
            "course": {
                "courseId": "course",
                "environment": "dev",
                "bucket": "channels-data-dev",
            },
            "results": [
                {
                    "topicId": "topic",
                    "topicTitle": "Topic",
                    "pdfKey": self.key,
                    "originalPdf": "original.pdf",
                    "reSweptPdf": "fixed.pdf",
                    "status": "swept",
                    "secondPass": {"byteStable": True},
                }
            ],
        }
        self._write_manifest()

        report = publish_manifest(
            self.manifest_path,
            self.report_path,
            self.root,
            self._s3(),
            apply=False,
        )

        self.assertEqual(report["summary"]["planned"], 1)

    def test_duplicate_keys_are_rejected(self) -> None:
        duplicate = dict(self.manifest["topics"][0])
        duplicate["topicId"] = "other"
        self.manifest["topics"].append(duplicate)
        self._write_manifest()

        with self.assertRaisesRegex(ReplacementError, "Invalid or duplicate"):
            publish_manifest(
                self.manifest_path,
                self.report_path,
                self.root,
                self._s3(),
                apply=False,
            )

    def test_verified_backup_can_restore_replacement(self) -> None:
        s3 = self._s3()
        publish_manifest(
            self.manifest_path,
            self.report_path,
            self.root,
            s3,
            apply=True,
            approved_manifest_sha256=manifest_sha256(self.manifest_path),
        )
        rollback_path = self.root / "rollback.json"
        invalidations: list[list[str]] = []

        result = rollback_report(
            self.report_path,
            rollback_path,
            s3,
            invalidate=lambda paths: invalidations.append(paths) or "rollback",
        )

        self.assertEqual(result["summary"]["restored"], 1)
        self.assertEqual(s3.objects[self.key]["body"], self.original)
        self.assertEqual(invalidations, [[f"/{self.key}"]])


if __name__ == "__main__":
    unittest.main()
