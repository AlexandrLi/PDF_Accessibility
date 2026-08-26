"""Tests for nonblocking Adobe residual-report telemetry."""

from __future__ import annotations

import io
import json
import unittest

from lib.adobe_residual_report import (
    TRACKED_CATEGORIES,
    after_report_key,
    before_report_key,
    compare_adobe_reports,
    load_adobe_reports,
    parse_adobe_report,
)


def _report(
    *,
    statuses: dict[str, str] | None = None,
    summary_failed: int = 8,
) -> dict:
    statuses = statuses or {category: "Passed" for category in TRACKED_CATEGORIES}
    details: dict[str, list[dict[str, str]]] = {}
    for category, (section, rule) in TRACKED_CATEGORIES.items():
        details.setdefault(section, []).append(
            {
                "Rule": rule,
                "Status": statuses.get(category, "Passed"),
                "Description": f"Description for {category}",
            }
        )
    return {"Summary": {"Failed": summary_failed, "Passed": 1}, "Detailed Report": details}


class FakeS3:
    def __init__(self, objects: dict[str, bytes], failures: dict[str, Exception] | None = None):
        self.objects = objects
        self.failures = failures or {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        del Bucket
        if Key in self.failures:
            raise self.failures[Key]
        if Key not in self.objects:
            error = RuntimeError("missing")
            error.response = {"Error": {"Code": "NoSuchKey"}}
            raise error
        return {"Body": io.BytesIO(self.objects[Key])}


class AdobeResidualReportTests(unittest.TestCase):
    def test_real_schema_maps_all_eight_rules_and_statuses(self) -> None:
        statuses = {
            "G": "Failed",
            "H": "Passed",
            "I": "Needs manual check",
            "J": "Failed",
            "K": "Passed",
            "Z": "Failed",
            "AA": "Passed",
            "AD": "Needs manual check",
        }
        payload = _report(statuses=statuses)
        payload["Summary"]["Description"] = "The checker found problems."
        parsed = parse_adobe_report(json.dumps(payload).encode())

        self.assertEqual(set(parsed.tracked_categories), set(TRACKED_CATEGORIES))
        self.assertEqual(parsed.failed_tracked_categories, ["G", "J", "Z"])
        self.assertEqual(len(parsed.manual_review_rules), 2)
        self.assertEqual(len(parsed.rules), 8)
        self.assertEqual(parsed.summary_counts["Failed"], 8)
        self.assertEqual(parsed.parser_diagnostics, [])

    def test_matching_is_case_and_space_tolerant_but_rule_matching_is_exact(self) -> None:
        payload = _report()
        tables = payload["Detailed Report"].pop("Tables")
        tables[0]["Rule"] = "  HEADERS "
        tables.append(
            {"Rule": "Headers and footers", "Status": "Failed", "Description": "other"}
        )
        payload["Detailed Report"][" Tables "] = tables
        parsed = parse_adobe_report(payload)

        self.assertEqual(parsed.tracked_categories["Z"].status, "Passed")
        self.assertEqual(parsed.tracked_categories["Z"].statuses, ["Passed"])
        self.assertEqual(parsed.tracked_categories["Z"].duplicate, False)

    def test_missing_malformed_and_duplicate_rules_are_explicit(self) -> None:
        payload = _report()
        del payload["Detailed Report"]["Document"]
        payload["Detailed Report"]["Tables"][0]["Status"] = 7
        payload["Detailed Report"]["Tables"].append(
            {"Rule": "Headers", "Status": "Passed", "Description": "duplicate"}
        )
        parsed = parse_adobe_report(payload)

        self.assertIn("G", parsed.missing_tracked_rules)
        self.assertIn("Z", parsed.malformed_tracked_rules)
        self.assertIn("Z", parsed.duplicate_tracked_rules)
        self.assertFalse(parsed.tracked_categories["Z"].valid)
        self.assertTrue(parsed.parser_diagnostics)

    def test_malformed_json_is_diagnostic_and_json_serializable(self) -> None:
        parsed = parse_adobe_report(b"{not json")

        self.assertTrue(parsed.parser_diagnostics)
        self.assertEqual(parsed.missing_tracked_rules, list(TRACKED_CATEGORIES))
        json.dumps(parsed.to_dict())

    def test_comparison_keeps_unknowns_and_reports_transitions(self) -> None:
        before = parse_adobe_report(
            _report(statuses={category: "Failed" for category in TRACKED_CATEGORIES})
        )
        after = parse_adobe_report(
            _report(
                statuses={
                    **{category: "Passed" for category in TRACKED_CATEGORIES},
                    "I": "Needs manual check",
                },
                summary_failed=1,
            )
        )
        after.tracked_categories["AA"].status = None
        after.tracked_categories["AA"].present = False
        comparison = compare_adobe_reports(before, after)

        self.assertEqual(comparison.columns["G"]["transition"], "resolved")
        self.assertEqual(comparison.columns["I"]["transition"], "failed_to_needsmanualcheck")
        self.assertEqual(comparison.columns["AA"]["transition"], "unknown")
        self.assertEqual(comparison.after_total_failed, 1)
        self.assertTrue(
            any(
                item["rule"] == "Tagged annotations"
                for item in comparison.manual_review_items
            )
        )
        self.assertEqual(comparison.residual_failures, [])
        json.dumps(comparison.to_dict())

    def test_loader_handles_both_reports_missing_and_access_errors(self) -> None:
        topic_id = "topic-1"
        before = before_report_key(topic_id)
        after = after_report_key(topic_id)
        access_error = RuntimeError("denied")
        access_error.response = {"Error": {"Code": "AccessDenied"}}
        telemetry = load_adobe_reports(
            FakeS3({before: json.dumps(_report()).encode()}, {after: access_error}),
            "bucket",
            topic_id,
        )

        self.assertTrue(telemetry.before.available)
        self.assertFalse(telemetry.after.available)
        self.assertEqual(telemetry.after.error["code"], "access_error")
        self.assertFalse(telemetry.to_dict()["publicationBlocked"])
        json.dumps(telemetry.to_dict())

        missing = load_adobe_reports(FakeS3({}), "bucket", topic_id)
        self.assertEqual(missing.before.error["code"], "missing")
        self.assertEqual(missing.after.error["code"], "missing")

    def test_loader_loads_both_known_report_keys(self) -> None:
        topic_id = "topic-both"
        telemetry = load_adobe_reports(
            FakeS3(
                {
                    before_report_key(topic_id): json.dumps(
                        _report(
                            statuses={
                                category: "Failed" for category in TRACKED_CATEGORIES
                            }
                        )
                    ).encode(),
                    after_report_key(topic_id): json.dumps(
                        _report(
                            statuses={
                                category: "Passed" for category in TRACKED_CATEGORIES
                            },
                            summary_failed=0,
                        )
                    ).encode(),
                }
            ),
            "bucket",
            topic_id,
        )

        self.assertTrue(telemetry.before.available)
        self.assertTrue(telemetry.after.available)
        self.assertEqual(telemetry.comparison.columns["AA"]["transition"], "resolved")
        self.assertEqual(telemetry.comparison.after_total_failed, 0)

    def test_loader_reports_malformed_json_without_raising(self) -> None:
        topic_id = "topic-2"
        telemetry = load_adobe_reports(
            FakeS3({before_report_key(topic_id): b"not json"}),
            "bucket",
            topic_id,
        )

        self.assertFalse(telemetry.before.available)
        self.assertEqual(telemetry.before.error["code"], "malformed")
        self.assertEqual(telemetry.comparison.columns["G"]["transition"], "unknown")


if __name__ == "__main__":
    unittest.main()
