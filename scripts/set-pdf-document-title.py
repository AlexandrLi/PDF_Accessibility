#!/usr/bin/env python3
"""Set document title metadata (docinfo, XMP, DisplayDocTitle) on one PDF."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lib.accessibility_course_workflow import render_hashes, validate_pdf
from lib.adobe_autotag import apply_document_title


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--output", required=True)
    parser.add_argument("--title", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    original = Path(args.input).read_bytes()
    titled = apply_document_title(original, args.title)
    if validate_pdf(titled)["pages"] != validate_pdf(original)["pages"]:
        raise ValueError("Title metadata update changed the page count")
    if render_hashes(titled) != render_hashes(original):
        raise ValueError("Title metadata update changed rendered page output")
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(titled)
    print(
        json.dumps(
            {"output": str(output_path), "title": args.title},
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
