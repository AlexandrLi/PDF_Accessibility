"""Parse and compare Adobe accessibility reports without blocking publication."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

TRACKED_CATEGORIES: dict[str, tuple[str, str]] = {
    "G": ("Document", "Bookmarks"),
    "H": ("Page Content", "Tagged content"),
    "I": ("Page Content", "Tagged annotations"),
    "J": ("Page Content", "Tab order"),
    "K": ("Page Content", "Character encoding"),
    "Z": ("Tables", "Headers"),
    "AA": ("Tables", "Regularity"),
    "AD": ("Headings", "Appropriate nesting"),
}

PIPELINE_AFTER_LABEL = "pipeline-post-remediation-before-local-sweeps"
_SUMMARY_COUNT_NAMES = frozenset(
    {
        "Needs manual check",
        "Passed manually",
        "Failed manually",
        "Skipped",
        "Passed",
        "Failed",
    }
)


def _normalized(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(value.split()).casefold()


def _status_key(value: str | None) -> str:
    return _normalized(value)


@dataclass
class AdobeRule:
    section: str
    rule: str | None
    status: str | None
    description: str | None
    valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrackedRuleStatus:
    category: str
    section: str
    rule: str
    status: str | None = None
    statuses: list[str] = field(default_factory=list)
    present: bool = False
    valid: bool = False
    duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdobeReport:
    summary_counts: dict[str, int | None] = field(default_factory=dict)
    rules: list[AdobeRule] = field(default_factory=list)
    tracked_categories: dict[str, TrackedRuleStatus] = field(default_factory=dict)
    failed_tracked_categories: list[str] = field(default_factory=list)
    manual_review_rules: list[dict[str, Any]] = field(default_factory=list)
    parser_diagnostics: list[str] = field(default_factory=list)
    missing_tracked_rules: list[str] = field(default_factory=list)
    malformed_tracked_rules: list[str] = field(default_factory=list)
    duplicate_tracked_rules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rules"] = [rule.to_dict() for rule in self.rules]
        payload["tracked_categories"] = {
            category: status.to_dict()
            for category, status in self.tracked_categories.items()
        }
        return payload


@dataclass
class AdobeReportLoad:
    key: str
    available: bool
    report: AdobeReport | None = None
    error: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "available": self.available,
            "report": self.report.to_dict() if self.report else None,
            "error": self.error,
        }


@dataclass
class AdobeResidualComparison:
    before_available: bool
    after_available: bool
    columns: dict[str, dict[str, Any]]
    before_total_failed: int | None
    after_total_failed: int | None
    before_tracked_failed_count: int | None
    after_tracked_failed_count: int | None
    residual_failures: list[str]
    manual_review_items: list[dict[str, Any]]
    before_manual_review_items: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdobeResidualTelemetry:
    before: AdobeReportLoad
    after: AdobeReportLoad
    comparison: AdobeResidualComparison
    after_report_semantics: str = PIPELINE_AFTER_LABEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "comparison": self.comparison.to_dict(),
            "afterReportSemantics": self.after_report_semantics,
            "publicationBlocked": False,
        }


def _count(value: Any, diagnostics: list[str], name: str) -> int | None:
    if isinstance(value, bool):
        diagnostics.append(f"Summary count {name!r} is boolean")
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    diagnostics.append(f"Summary count {name!r} is not an integer")
    return None


def parse_adobe_report(source: bytes | str | Mapping[str, Any]) -> AdobeReport:
    """Parse Adobe JSON data, returning diagnostics instead of raising."""
    diagnostics: list[str] = []
    root: Mapping[str, Any] | None = None
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError as error:
            diagnostics.append(f"Report is not UTF-8: {error}")
    if isinstance(source, str):
        try:
            decoded = json.loads(source)
        except (json.JSONDecodeError, TypeError) as error:
            diagnostics.append(f"Report JSON is malformed: {error}")
            decoded = None
        if isinstance(decoded, Mapping):
            root = decoded
        elif decoded is not None:
            diagnostics.append("Report JSON root is not an object")
    elif isinstance(source, Mapping):
        root = source
    elif not isinstance(source, bytes):
        diagnostics.append("Report input must be bytes, string, or object")

    report = AdobeReport(parser_diagnostics=diagnostics)
    for category, (section, rule) in TRACKED_CATEGORIES.items():
        report.tracked_categories[category] = TrackedRuleStatus(
            category=category,
            section=section,
            rule=rule,
        )

    if root is None:
        report.missing_tracked_rules = list(TRACKED_CATEGORIES)
        return report

    summary = root.get("Summary")
    if isinstance(summary, Mapping):
        for name, value in summary.items():
            if isinstance(name, str) and any(
                _normalized(name) == _normalized(expected)
                for expected in _SUMMARY_COUNT_NAMES
            ):
                report.summary_counts[name] = _count(value, diagnostics, name)
    elif summary is None:
        diagnostics.append("Summary is missing")
    else:
        diagnostics.append("Summary is not an object")

    detailed = root.get("Detailed Report")
    if not isinstance(detailed, Mapping):
        diagnostics.append("Detailed Report is missing or is not an object")
        report.missing_tracked_rules = list(TRACKED_CATEGORIES)
        return report

    matched: dict[str, list[AdobeRule]] = {category: [] for category in TRACKED_CATEGORIES}
    for section_name, section_rules in detailed.items():
        if not isinstance(section_name, str):
            diagnostics.append("Detailed Report contains a non-string section name")
            continue
        if not isinstance(section_rules, list):
            diagnostics.append(f"Section {section_name!r} is not an array")
            continue
        for raw_rule in section_rules:
            if not isinstance(raw_rule, Mapping):
                diagnostics.append(f"Section {section_name!r} contains a malformed rule")
                report.rules.append(
                    AdobeRule(section_name, None, None, None, valid=False)
                )
                continue
            rule_value = raw_rule.get("Rule")
            status_value = raw_rule.get("Status")
            description_value = raw_rule.get("Description")
            valid = isinstance(rule_value, str) and bool(rule_value.strip())
            if "Status" not in raw_rule or not isinstance(status_value, str):
                valid = False
            if "Description" in raw_rule and not isinstance(description_value, str):
                valid = False
            if not valid:
                diagnostics.append(
                    f"Malformed rule in section {section_name!r}: {rule_value!r}"
                )
            parsed = AdobeRule(
                section=section_name,
                rule=rule_value if isinstance(rule_value, str) else None,
                status=status_value if isinstance(status_value, str) else None,
                description=(
                    description_value if isinstance(description_value, str) else None
                ),
                valid=valid,
            )
            report.rules.append(parsed)
            for category, (expected_section, expected_rule) in TRACKED_CATEGORIES.items():
                if (
                    _normalized(section_name) == _normalized(expected_section)
                    and _normalized(rule_value) == _normalized(expected_rule)
                ):
                    matched[category].append(parsed)

            if isinstance(status_value, str) and _status_key(status_value) == "needsmanualcheck":
                report.manual_review_rules.append(parsed.to_dict())

    for category, matches in matched.items():
        tracked = report.tracked_categories[category]
        tracked.present = bool(matches)
        tracked.statuses = [match.status for match in matches if match.status is not None]
        tracked.duplicate = len(matches) > 1
        tracked.valid = len(matches) == 1 and matches[0].valid
        if matches:
            tracked.status = matches[0].status
        if not matches:
            report.missing_tracked_rules.append(category)
        elif len(matches) > 1:
            report.duplicate_tracked_rules.append(category)
            diagnostics.append(f"Tracked rule {category} appears {len(matches)} times")
        if matches and not all(match.valid for match in matches):
            report.malformed_tracked_rules.append(category)
        if _status_key(tracked.status) == "failed" and tracked.valid:
            report.failed_tracked_categories.append(category)

    return report


def _report_from_value(value: AdobeReport | Mapping[str, Any] | None) -> AdobeReport | None:
    if value is None:
        return value
    if isinstance(value, AdobeReport):
        return None if _has_unreadable_parser_error(value) else value
    return parse_adobe_report(value)


def _status_for(report: AdobeReport | None, category: str) -> tuple[str | None, bool]:
    if report is None:
        return None, False
    tracked = report.tracked_categories.get(category)
    if tracked is None:
        return None, False
    return tracked.status, tracked.present and tracked.valid and not tracked.duplicate


def _summary_count(report: AdobeReport | None, name: str) -> int | None:
    if report is None:
        return None
    expected = _normalized(name)
    for key, value in report.summary_counts.items():
        if _normalized(key) == expected:
            return value
    return None


def _has_unreadable_parser_error(report: AdobeReport) -> bool:
    unreadable_prefixes = (
        "Report is not UTF-8",
        "Report JSON is malformed",
        "Report JSON root is not an object",
        "Report input must",
        "Summary is missing",
        "Summary is not an object",
        "Detailed Report is missing",
    )
    return any(
        diagnostic.startswith(unreadable_prefixes)
        for diagnostic in report.parser_diagnostics
    )


def _transition(before: str | None, after: str | None, before_known: bool, after_known: bool) -> str:
    if not before_known or not after_known:
        return "unknown"
    before_key = _status_key(before)
    after_key = _status_key(after)
    if before_key == after_key:
        return "unchanged"
    if before_key == "failed" and after_key == "passed":
        return "resolved"
    if before_key == "passed" and after_key == "failed":
        return "regressed"
    if after_key == "failed":
        return "residual-failure"
    return f"{before_key or 'unknown'}_to_{after_key or 'unknown'}"


def compare_adobe_reports(
    before: AdobeReport | Mapping[str, Any] | None,
    after: AdobeReport | Mapping[str, Any] | None,
) -> AdobeResidualComparison:
    """Compare reports while retaining unknowns for absent or invalid rules."""
    before_report = _report_from_value(before)
    after_report = _report_from_value(after)
    columns: dict[str, dict[str, Any]] = {}
    residual: list[str] = []
    for category, (section, rule) in TRACKED_CATEGORIES.items():
        before_status, before_known = _status_for(before_report, category)
        after_status, after_known = _status_for(after_report, category)
        transition = _transition(before_status, after_status, before_known, after_known)
        columns[category] = {
            "category": category,
            "section": section,
            "rule": rule,
            "beforeStatus": before_status,
            "afterStatus": after_status,
            "beforeKnown": before_known,
            "afterKnown": after_known,
            "transition": transition,
        }
        if after_known and _status_key(after_status) == "failed":
            residual.append(category)

    before_manual = before_report.manual_review_rules if before_report else []
    after_manual = after_report.manual_review_rules if after_report else []
    return AdobeResidualComparison(
        before_available=before_report is not None,
        after_available=after_report is not None,
        columns=columns,
        before_total_failed=(
            _summary_count(before_report, "Failed")
        ),
        after_total_failed=(
            _summary_count(after_report, "Failed")
        ),
        before_tracked_failed_count=(
            len(before_report.failed_tracked_categories) if before_report else None
        ),
        after_tracked_failed_count=(
            len(after_report.failed_tracked_categories) if after_report else None
        ),
        residual_failures=residual,
        manual_review_items=after_manual,
        before_manual_review_items=before_manual,
    )


def before_report_key(topic_id: str) -> str:
    return f"temp/{topic_id}/accessability-report/{topic_id}_accessibility_report_before_remidiation.json"


def after_report_key(topic_id: str) -> str:
    return (
        f"temp/{topic_id}/accessability-report/"
        f"COMPLIANT_{topic_id}_accessibility_report_after_remidiation.json"
    )


def _load_one_report(s3_client, bucket: str, key: str) -> AdobeReportLoad:
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        payload = body.read() if hasattr(body, "read") else body
    except Exception as error:
        error_response = getattr(error, "response", {})
        error_info = (
            error_response.get("Error", {})
            if isinstance(error_response, Mapping)
            else {}
        )
        code = str(error_info.get("Code", "ClientError"))
        if code in {"404", "NoSuchKey", "NotFound"} or isinstance(error, KeyError):
            return AdobeReportLoad(key, False, error={"code": "missing", "message": code})
        if not isinstance(error_response, Mapping):
            return AdobeReportLoad(
                key, False, error={"code": "unavailable", "message": str(error)}
            )
        return AdobeReportLoad(key, False, error={"code": "access_error", "message": str(error)})

    report = parse_adobe_report(payload)
    if _has_unreadable_parser_error(report):
        return AdobeReportLoad(
            key,
            False,
            report=report,
            error={
                "code": "malformed",
                "kind": "parser_error",
                "message": "; ".join(report.parser_diagnostics),
            },
        )
    return AdobeReportLoad(key, True, report=report)


def load_adobe_reports(s3_client, bucket: str, topic_id: str) -> AdobeResidualTelemetry:
    """Load both known report keys; every S3/parse failure is telemetry."""
    before_key = before_report_key(topic_id)
    after_key = after_report_key(topic_id)
    try:
        before = _load_one_report(s3_client, bucket, before_key)
    except Exception as error:
        before = AdobeReportLoad(
            before_key,
            False,
            error={"code": "unavailable", "message": str(error)},
        )
    try:
        after = _load_one_report(s3_client, bucket, after_key)
    except Exception as error:
        after = AdobeReportLoad(
            after_key,
            False,
            error={"code": "unavailable", "message": str(error)},
        )
    try:
        comparison = compare_adobe_reports(
            before.report if before.available else None,
            after.report if after.available else None,
        )
    except Exception as error:
        comparison = compare_adobe_reports(None, None)
        before.error = before.error or {
            "code": "comparison_error",
            "message": str(error),
        }
    return AdobeResidualTelemetry(
        before=before,
        after=after,
        comparison=comparison,
    )


# Descriptive aliases for callers that prefer the report terminology.
load_adobe_residual_telemetry = load_adobe_reports
parse_report = parse_adobe_report
compare_reports = compare_adobe_reports
