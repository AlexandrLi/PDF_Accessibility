#!/usr/bin/env python3
"""Run Adobe PDF Services accessibility checker for one local PDF."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lib.adobe_autotag import check_pdf_accessibility_from_secret


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--report", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    report_path = Path(args.report)
    report = check_pdf_accessibility_from_secret(input_path.read_bytes())
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(report)
    payload = json.loads(report)
    print(
        json.dumps(
            {
                "report": str(report_path),
                "status": payload.get("status"),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
