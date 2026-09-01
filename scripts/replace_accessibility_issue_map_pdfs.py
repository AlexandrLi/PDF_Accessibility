#!/usr/bin/env python3
"""Plan, apply, or roll back fail-closed accessibility PDF replacements."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3

from lib.cloudfront import invalidate_paths
from lib.config import cloudfront_distribution_id
from lib.s3_pdf_replacement import (
    manifest_sha256,
    publish_manifest,
    rollback_report,
)


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("plan", "apply"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--manifest", required=True)
        subparser.add_argument("--report")
        subparser.add_argument(
            "--approved-exception",
            action="append",
            default=[],
            metavar="TOPIC_ID",
            help="Explicitly allow one manually verified non-resolved topic",
        )
        subparser.add_argument(
            "--approved-render-exception",
            action="append",
            default=[],
            metavar="TOPIC_ID",
            help=(
                "Explicitly allow one visually reviewed topic whose repaired "
                "PDF renders differently from the original"
            ),
        )
        if command == "apply":
            subparser.add_argument("--approved-sha", required=True)

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--report", required=True)
    rollback.add_argument("--output")
    return parser.parse_args()


def _default_report(manifest: Path, *, apply: bool) -> Path:
    if not apply:
        return manifest.with_name(f"{manifest.stem}.replacement-plan.json")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    course_id = manifest.stem.split("-issue-map", 1)[0]
    return ROOT / "reports" / f"{course_id}-s3-replacement-audit-{timestamp}.json"


def main() -> int:
    args = parse_args()
    s3 = boto3.client("s3")
    if args.command == "rollback":
        source = Path(args.report)
        output = (
            Path(args.output)
            if args.output
            else source.with_name(f"{source.stem}.rollback.json")
        )
        replacement_report = json.loads(source.read_text(encoding="utf-8"))
        environment = str(
            (replacement_report.get("course") or {}).get("environment") or "dev"
        )
        distribution_id = cloudfront_distribution_id(environment)

        def invalidate_rollback(paths: list[str]) -> str | None:
            return invalidate_paths(distribution_id, paths)

        result = rollback_report(
            source,
            output,
            s3,
            invalidate=invalidate_rollback,
        )
        print(
            json.dumps(
                {"report": str(output), "summary": result["summary"]},
                separators=(",", ":"),
            )
        )
        return 1 if result["summary"]["failed"] else 0

    manifest = Path(args.manifest)
    report = (
        Path(args.report)
        if args.report
        else _default_report(manifest, apply=args.command == "apply")
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    environment = str((payload.get("course") or {}).get("environment") or "dev")
    distribution_id = cloudfront_distribution_id(environment)

    def invalidate(paths: list[str]) -> str | None:
        return invalidate_paths(distribution_id, paths)

    result = publish_manifest(
        manifest,
        report,
        ROOT,
        s3,
        apply=args.command == "apply",
        approved_manifest_sha256=(
            args.approved_sha if args.command == "apply" else None
        ),
        approved_exceptions=set(args.approved_exception),
        approved_render_exceptions=set(args.approved_render_exception),
        invalidate=invalidate if args.command == "apply" else None,
    )
    print(
        json.dumps(
            {
                "report": str(report),
                "manifestSha256": manifest_sha256(manifest),
                "summary": result["summary"],
                "outcome": result["outcome"],
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
