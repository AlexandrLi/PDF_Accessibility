"""Fail-closed S3 replacement for manually approved accessibility PDFs."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from lib.accessibility_course_workflow import render_hashes, validate_pdf
from lib.config import channels_bucket


REPORT_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 2
PRESERVED_HEAD_FIELDS = (
    "CacheControl",
    "ContentDisposition",
    "ContentEncoding",
    "ContentLanguage",
    "ContentType",
    "Expires",
    "WebsiteRedirectLocation",
    "Metadata",
    "ServerSideEncryption",
    "SSEKMSKeyId",
    "BucketKeyEnabled",
)


class ReplacementError(RuntimeError):
    """Raised when replacement cannot preserve the fail-closed contract."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_sha256(path: Path) -> str:
    return file_sha256(path)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


def _legacy_result_kind(status: object) -> str | None:
    return {
        "swept": "resolved",
        "swept-with-warnings": "resolved",
        "swept-with-residuals": "residual",
        "swept-unverifiable": "unverifiable",
    }.get(str(status or ""))


def _normalize_legacy_manifest(
    payload: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    results = payload.get("results")
    if not isinstance(results, list) or not isinstance(payload.get("course"), dict):
        raise ReplacementError("Unsupported replacement manifest schema")
    topics: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict) or not result.get("topicId"):
            continue
        original_path = _safe_local_path(root, result.get("originalPdf"))
        fixed_path = _safe_local_path(root, result.get("reSweptPdf"))
        if not original_path.is_file() or not fixed_path.is_file():
            raise ReplacementError(
                f"{result.get('topicId')}: legacy manifest local PDF is missing"
            )
        original = original_path.read_bytes()
        fixed = fixed_path.read_bytes()
        topics.append(
            {
                "topicId": result.get("topicId"),
                "topicTitle": result.get("topicTitle"),
                "pdfKey": result.get("pdfKey"),
                "originalPdf": result.get("originalPdf"),
                "reSweptPdf": result.get("reSweptPdf"),
                "originalSha256": _sha256(original),
                "reSweptSha256": _sha256(fixed),
                "resultKind": result.get("resultKind")
                or _legacy_result_kind(result.get("status")),
                "renderIdentical": render_hashes(original) == render_hashes(fixed),
                "secondPassByteStable": (result.get("secondPass") or {}).get(
                    "byteStable"
                )
                is True,
            }
        )
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "course": payload["course"],
        "topics": topics,
        "legacySource": True,
    }


def _load_manifest(path: Path, root: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
        payload = _normalize_legacy_manifest(payload, root)
    if not isinstance(payload.get("topics"), list):
        raise ReplacementError("Manifest topics must be a list")
    return payload


def _safe_local_path(root: Path, value: object) -> Path:
    candidate = (root / str(value or "")).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise ReplacementError(f"Path escapes repository root: {value!r}") from error
    return candidate


def _body_bytes(s3: Any, bucket: str, key: str, version_id: str | None = None) -> bytes:
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id and version_id != "null":
        kwargs["VersionId"] = version_id
    return s3.get_object(**kwargs)["Body"].read()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _head_properties(head: dict[str, Any]) -> dict[str, Any]:
    return {
        field: head[field]
        for field in PRESERVED_HEAD_FIELDS
        if head.get(field) is not None
    }


def _put_properties(properties: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "CacheControl",
        "ContentDisposition",
        "ContentEncoding",
        "ContentLanguage",
        "ContentType",
        "Expires",
        "WebsiteRedirectLocation",
        "Metadata",
        "ServerSideEncryption",
        "SSEKMSKeyId",
        "BucketKeyEnabled",
    }
    return {key: value for key, value in properties.items() if key in allowed}


def _tags(s3: Any, bucket: str, key: str, version_id: str | None = None) -> list[dict]:
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id and version_id != "null":
        kwargs["VersionId"] = version_id
    return list(s3.get_object_tagging(**kwargs).get("TagSet") or [])


def _acl(s3: Any, bucket: str, key: str, version_id: str | None = None) -> dict:
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id and version_id != "null":
        kwargs["VersionId"] = version_id
    response = s3.get_object_acl(**kwargs)
    return {
        "Owner": response.get("Owner"),
        "Grants": response.get("Grants") or [],
    }


def _copy_source(
    bucket: str,
    key: str,
    version_id: str | None = None,
) -> dict[str, str]:
    source = {"Bucket": bucket, "Key": key}
    if version_id and version_id != "null":
        source["VersionId"] = version_id
    return source


def _properties_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def _empty_report(
    manifest_path: Path,
    manifest_digest: str,
    manifest: dict[str, Any],
    *,
    apply: bool,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    course = manifest.get("course") or {}
    return {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "runId": f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}",
        "runAt": now.isoformat(),
        "mode": "apply" if apply else "plan",
        "manifest": str(manifest_path),
        "manifestSha256": manifest_digest,
        "course": {
            "courseId": course.get("courseId"),
            "environment": course.get("environment"),
            "bucket": course.get("bucket"),
        },
        "bucketVersioning": None,
        "objects": [],
        "cloudFront": {"requestedPaths": [], "invalidationId": None},
        "summary": {
            "requested": len(manifest.get("topics") or []),
            "planned": 0,
            "alreadyApplied": 0,
            "backedUp": 0,
            "uploaded": 0,
            "verified": 0,
            "failed": 0,
            "recoverable": 0,
        },
        "outcome": "planning",
    }


def _validate_manifest_scope(
    manifest: dict[str, Any],
    root: Path,
    approved_exceptions: set[str],
    approved_render_exceptions: set[str] | None = None,
) -> None:
    render_exceptions = approved_render_exceptions or set()
    course = manifest.get("course") or {}
    course_id = str(course.get("courseId") or "")
    bucket = str(course.get("bucket") or "")
    environment = str(course.get("environment") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", course_id):
        raise ReplacementError(f"Invalid courseId: {course_id!r}")
    if environment not in {"dev", "prod"}:
        raise ReplacementError(f"Invalid environment: {environment!r}")
    if not bucket:
        raise ReplacementError("Manifest courseId and bucket are required")
    if bucket != channels_bucket(environment):
        raise ReplacementError("Manifest bucket does not match its environment")
    prefix = f"courses/{course_id}/topic_pdfs/"
    seen_keys: set[str] = set()
    seen_topics: set[str] = set()
    for topic in manifest["topics"]:
        topic_id = str(topic.get("topicId") or "")
        key = str(topic.get("pdfKey") or "")
        if not topic_id or topic_id in seen_topics:
            raise ReplacementError(f"Missing or duplicate topicId: {topic_id!r}")
        if key != f"{prefix}{topic_id}.pdf" or key in seen_keys:
            raise ReplacementError(f"Invalid or duplicate preview key: {key!r}")
        if (
            topic.get("resultKind") != "resolved"
            and topic_id not in approved_exceptions
        ):
            raise ReplacementError(
                f"{topic_id}: manual publication requires resolved status"
            )
        if (
            topic.get("renderIdentical") is not True
            and topic_id not in render_exceptions
        ):
            raise ReplacementError(f"{topic_id}: rendering was not verified")
        if topic.get("secondPassByteStable") is not True:
            raise ReplacementError(f"{topic_id}: second pass was not stable")
        for field in ("originalPdf", "reSweptPdf"):
            _safe_local_path(root, topic.get(field))
        seen_topics.add(topic_id)
        seen_keys.add(key)
    unknown_exceptions = approved_exceptions - seen_topics
    if unknown_exceptions:
        raise ReplacementError(
            f"Approved exception topic IDs are not in the manifest: {sorted(unknown_exceptions)}"
        )
    unknown_render_exceptions = render_exceptions - seen_topics
    if unknown_render_exceptions:
        raise ReplacementError(
            "Approved render exception topic IDs are not in the manifest: "
            f"{sorted(unknown_render_exceptions)}"
        )


def _preflight_topic(
    s3: Any,
    bucket: str,
    root: Path,
    topic: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    topic_id = str(topic["topicId"])
    key = str(topic["pdfKey"])
    original_path = _safe_local_path(root, topic["originalPdf"])
    fixed_path = _safe_local_path(root, topic["reSweptPdf"])
    if not original_path.is_file() or not fixed_path.is_file():
        raise ReplacementError(f"{topic_id}: local original or fixed PDF is missing")
    original_bytes = original_path.read_bytes()
    fixed_bytes = fixed_path.read_bytes()
    original_validation = validate_pdf(original_bytes)
    fixed_validation = validate_pdf(fixed_bytes)
    if original_validation["sha256"] != topic.get("originalSha256"):
        raise ReplacementError(f"{topic_id}: local original hash mismatch")
    if fixed_validation["sha256"] != topic.get("reSweptSha256"):
        raise ReplacementError(f"{topic_id}: local fixed hash mismatch")
    if original_validation["pages"] != fixed_validation["pages"]:
        raise ReplacementError(f"{topic_id}: page count changed")

    head = s3.head_object(Bucket=bucket, Key=key)
    version_id = str(head.get("VersionId") or "")
    remote_bytes = _body_bytes(s3, bucket, key, version_id)
    remote_hash = _sha256(remote_bytes)
    if remote_hash == fixed_validation["sha256"]:
        state = "already-applied"
    elif remote_hash == original_validation["sha256"]:
        state = "planned"
    else:
        raise ReplacementError(f"{topic_id}: current S3 object has source drift")

    captured = {
        "properties": _head_properties(head),
        "tags": _tags(s3, bucket, key, version_id),
        "acl": _acl(s3, bucket, key, version_id),
        "versionId": version_id or None,
    }
    report_entry = {
        "topicId": topic_id,
        "topicTitle": topic.get("topicTitle"),
        "key": key,
        "status": state,
        "originalSha256": original_validation["sha256"],
        "replacementSha256": fixed_validation["sha256"],
        "originalBytes": len(original_bytes),
        "replacementBytes": len(fixed_bytes),
        "pages": fixed_validation["pages"],
        "sourceVersionId": version_id or None,
        "backupKey": None,
        "backupVerified": False,
        "backupPropertiesPreserved": False,
        "backupTagsPreserved": False,
        "backupAclPreserved": False,
        "propertiesPreserved": False,
        "tagsPreserved": False,
        "aclPreserved": False,
        "replacementVerified": state == "already-applied",
        "error": None,
    }
    private = {
        "fixedBytes": fixed_bytes,
        "captured": captured,
    }
    return report_entry, private


def _backup_topic(
    s3: Any,
    bucket: str,
    course_id: str,
    run_id: str,
    entry: dict[str, Any],
    captured: dict[str, Any],
) -> None:
    backup_key = (
        f"courses/{course_id}/topic_pdfs/"
        f".accessibility-replacement-backups/{run_id}/{entry['topicId']}.pdf"
    )
    s3.copy_object(
        Bucket=bucket,
        Key=backup_key,
        CopySource=_copy_source(
            bucket,
            entry["key"],
            entry.get("sourceVersionId"),
        ),
        MetadataDirective="COPY",
        TaggingDirective="COPY",
    )
    backup_bytes = _body_bytes(s3, bucket, backup_key)
    if _sha256(backup_bytes) != entry["originalSha256"]:
        raise ReplacementError(f"{entry['topicId']}: backup hash mismatch")
    validate_pdf(backup_bytes)
    backup_acl = _acl(s3, bucket, backup_key)
    if backup_acl != captured["acl"] and captured["acl"].get("Owner"):
        s3.put_object_acl(
            Bucket=bucket,
            Key=backup_key,
            AccessControlPolicy=captured["acl"],
        )
    backup_head = s3.head_object(Bucket=bucket, Key=backup_key)
    entry["backupPropertiesPreserved"] = _properties_match(
        captured["properties"],
        _head_properties(backup_head),
    )
    entry["backupTagsPreserved"] = (
        _tags(s3, bucket, backup_key) == captured["tags"]
    )
    entry["backupAclPreserved"] = _acl(s3, bucket, backup_key) == captured["acl"]
    if not all(
        (
            entry["backupPropertiesPreserved"],
            entry["backupTagsPreserved"],
            entry["backupAclPreserved"],
        )
    ):
        raise ReplacementError(
            f"{entry['topicId']}: backup properties were not preserved"
        )
    entry["backupKey"] = backup_key
    entry["backupVerified"] = True


def _restore_topic(
    s3: Any,
    bucket: str,
    entry: dict[str, Any],
) -> None:
    backup_key = entry.get("backupKey")
    version_id = entry.get("sourceVersionId")
    if backup_key:
        source = _copy_source(bucket, str(backup_key))
        recovery_key = str(backup_key)
        recovery_version = None
    elif version_id and version_id != "null":
        source = _copy_source(bucket, str(entry["key"]), str(version_id))
        recovery_key = str(entry["key"])
        recovery_version = str(version_id)
    else:
        raise ReplacementError(f"{entry['topicId']}: no verified recovery source")
    recovery_head = s3.head_object(
        **{
            "Bucket": bucket,
            "Key": recovery_key,
            **(
                {"VersionId": recovery_version}
                if recovery_version
                else {}
            ),
        }
    )
    recovery_properties = _head_properties(recovery_head)
    recovery_tags = _tags(s3, bucket, recovery_key, recovery_version)
    recovery_acl = _acl(s3, bucket, recovery_key, recovery_version)
    s3.copy_object(
        Bucket=bucket,
        Key=entry["key"],
        CopySource=source,
        MetadataDirective="COPY",
        TaggingDirective="COPY",
    )
    current_acl = _acl(s3, bucket, entry["key"])
    if current_acl != recovery_acl and recovery_acl.get("Owner"):
        s3.put_object_acl(
            Bucket=bucket,
            Key=entry["key"],
            AccessControlPolicy=recovery_acl,
        )
    restored = _body_bytes(s3, bucket, entry["key"])
    if _sha256(restored) != entry["originalSha256"]:
        raise ReplacementError(f"{entry['topicId']}: rollback hash mismatch")
    restored_head = s3.head_object(Bucket=bucket, Key=entry["key"])
    if not _properties_match(recovery_properties, _head_properties(restored_head)):
        raise ReplacementError(f"{entry['topicId']}: rollback properties mismatch")
    if _tags(s3, bucket, entry["key"]) != recovery_tags:
        raise ReplacementError(f"{entry['topicId']}: rollback tags mismatch")
    if _acl(s3, bucket, entry["key"]) != recovery_acl:
        raise ReplacementError(f"{entry['topicId']}: rollback ACL mismatch")


def publish_manifest(
    manifest_path: Path,
    report_path: Path,
    root: Path,
    s3: Any,
    *,
    apply: bool,
    approved_manifest_sha256: str | None = None,
    approved_exceptions: set[str] | None = None,
    approved_render_exceptions: set[str] | None = None,
    invalidate: Callable[[list[str]], str | None] | None = None,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    digest = manifest_sha256(manifest_path)
    manifest = _load_manifest(manifest_path, root)
    exceptions = approved_exceptions or set()
    render_exceptions = approved_render_exceptions or set()
    _validate_manifest_scope(manifest, root, exceptions, render_exceptions)
    if apply and approved_manifest_sha256 != digest:
        raise ReplacementError(
            "Approved manifest SHA-256 does not match the current manifest"
        )

    report = _empty_report(manifest_path, digest, manifest, apply=apply)
    report["approvedExceptions"] = sorted(exceptions)
    report["approvedRenderExceptions"] = sorted(render_exceptions)
    course = report["course"]
    bucket = str(course["bucket"])
    course_id = str(course["courseId"])
    private_by_topic: dict[str, dict[str, Any]] = {}
    try:
        for topic in manifest["topics"]:
            entry, private = _preflight_topic(s3, bucket, root, topic)
            report["objects"].append(entry)
            private_by_topic[entry["topicId"]] = private
            if entry["status"] == "already-applied":
                report["summary"]["alreadyApplied"] += 1
                report["summary"]["verified"] += 1
            else:
                report["summary"]["planned"] += 1
    except Exception as error:
        report["summary"]["failed"] += 1
        report["outcome"] = "preflight-failed"
        report["error"] = str(error)
        _write_report(report_path, report)
        raise

    if not apply:
        report["outcome"] = "planned"
        _write_report(report_path, report)
        return report

    versioning = s3.get_bucket_versioning(Bucket=bucket).get("Status") or "Disabled"
    report["bucketVersioning"] = versioning
    candidates = [
        entry for entry in report["objects"] if entry["status"] == "planned"
    ]
    try:
        if versioning != "Enabled":
            for entry in candidates:
                _backup_topic(
                    s3,
                    bucket,
                    course_id,
                    report["runId"],
                    entry,
                    private_by_topic[entry["topicId"]]["captured"],
                )
                report["summary"]["backedUp"] += 1
                report["summary"]["recoverable"] += 1
        else:
            for entry in candidates:
                if not entry.get("sourceVersionId"):
                    raise ReplacementError(
                        f"{entry['topicId']}: enabled versioning returned no version ID"
                    )
                report["summary"]["recoverable"] += 1
    except Exception as error:
        report["summary"]["failed"] += 1
        report["outcome"] = "backup-failed"
        report["error"] = str(error)
        _write_report(report_path, report)
        raise

    written: list[dict[str, Any]] = []
    try:
        for entry in candidates:
            private = private_by_topic[entry["topicId"]]
            captured = private["captured"]
            put_properties = _put_properties(captured["properties"])
            put_properties.setdefault("ContentType", "application/pdf")
            written.append(entry)
            s3.put_object(
                Bucket=bucket,
                Key=entry["key"],
                Body=private["fixedBytes"],
                **put_properties,
            )
            if captured["tags"]:
                s3.put_object_tagging(
                    Bucket=bucket,
                    Key=entry["key"],
                    Tagging={"TagSet": captured["tags"]},
                )
            current_acl = _acl(s3, bucket, entry["key"])
            if current_acl != captured["acl"] and captured["acl"].get("Owner"):
                s3.put_object_acl(
                    Bucket=bucket,
                    Key=entry["key"],
                    AccessControlPolicy=captured["acl"],
                )

            replacement = _body_bytes(s3, bucket, entry["key"])
            if _sha256(replacement) != entry["replacementSha256"]:
                raise ReplacementError(
                    f"{entry['topicId']}: replacement read-back hash mismatch"
                )
            validation = validate_pdf(replacement)
            if validation["pages"] != entry["pages"]:
                raise ReplacementError(
                    f"{entry['topicId']}: replacement page count mismatch"
                )
            new_head = s3.head_object(Bucket=bucket, Key=entry["key"])
            new_properties = _head_properties(new_head)
            new_tags = _tags(s3, bucket, entry["key"])
            new_acl = _acl(s3, bucket, entry["key"])
            entry["propertiesPreserved"] = _properties_match(
                captured["properties"],
                new_properties,
            )
            entry["tagsPreserved"] = new_tags == captured["tags"]
            entry["aclPreserved"] = new_acl == captured["acl"]
            if not all(
                (
                    entry["propertiesPreserved"],
                    entry["tagsPreserved"],
                    entry["aclPreserved"],
                )
            ):
                raise ReplacementError(
                    f"{entry['topicId']}: object properties were not preserved"
                )
            entry["replacementVerified"] = True
            entry["status"] = "verified"
            report["summary"]["uploaded"] += 1
            report["summary"]["verified"] += 1
            _write_report(report_path, report)
    except Exception as error:
        report["summary"]["failed"] += 1
        report["outcome"] = "replacement-failed"
        report["error"] = str(error)
        for entry in reversed(written):
            try:
                _restore_topic(s3, bucket, entry)
                entry["status"] = "rolled-back"
            except Exception as rollback_error:
                entry["error"] = f"rollback failed: {rollback_error}"
        report["summary"]["uploaded"] = sum(
            entry.get("status") == "verified"
            for entry in report["objects"]
        )
        report["summary"]["verified"] = (
            report["summary"]["alreadyApplied"] + report["summary"]["uploaded"]
        )
        _write_report(report_path, report)
        raise

    paths = [
        f"/{entry['key']}"
        for entry in report["objects"]
        if entry.get("status") in {"verified", "already-applied"}
    ]
    report["cloudFront"]["requestedPaths"] = paths
    if paths and invalidate:
        try:
            report["cloudFront"]["invalidationId"] = invalidate(paths)
        except Exception as error:
            report["cloudFront"]["error"] = str(error)
            report["summary"]["failed"] += 1
            report["outcome"] = "invalidation-failed"
            _write_report(report_path, report)
            raise ReplacementError(
                "Replacements verified, but CloudFront invalidation failed"
            ) from error
    report["outcome"] = "completed"
    _write_report(report_path, report)
    return report


def rollback_report(
    report_path: Path,
    output_path: Path,
    s3: Any,
    invalidate: Callable[[list[str]], str | None] | None = None,
) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schemaVersion") != REPORT_SCHEMA_VERSION:
        raise ReplacementError("Unsupported replacement report schema")
    bucket = str((report.get("course") or {}).get("bucket") or "")
    if not bucket:
        raise ReplacementError("Replacement report has no bucket")
    result = {
        "schemaVersion": 1,
        "rollbackOf": str(report_path),
        "runAt": datetime.now(timezone.utc).isoformat(),
        "objects": [],
        "summary": {"requested": 0, "restored": 0, "failed": 0},
        "cloudFront": {"requestedPaths": [], "invalidationId": None},
    }
    for entry in report.get("objects") or []:
        if entry.get("status") not in {"verified", "rolled-back"}:
            continue
        result["summary"]["requested"] += 1
        item = {"topicId": entry.get("topicId"), "key": entry.get("key")}
        try:
            _restore_topic(s3, bucket, entry)
            item["status"] = "restored"
            result["summary"]["restored"] += 1
        except Exception as error:
            item["status"] = "failed"
            item["error"] = str(error)
            result["summary"]["failed"] += 1
        result["objects"].append(item)
        _write_report(output_path, result)
    restored_paths = [
        f"/{item['key']}"
        for item in result["objects"]
        if item.get("status") == "restored"
    ]
    result["cloudFront"]["requestedPaths"] = restored_paths
    if restored_paths and invalidate:
        result["cloudFront"]["invalidationId"] = invalidate(restored_paths)
    _write_report(output_path, result)
    return result
