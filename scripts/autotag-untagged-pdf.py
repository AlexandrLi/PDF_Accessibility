#!/usr/bin/env python3
"""Auto-tag one untagged PDF with Adobe, then apply local accessibility sweeps."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lib.accessibility_course_workflow import (
    prepare_pdf,
    render_hashes,
    validate_pdf,
)
from lib.adobe_autotag import (
    autotag_pdf_from_secret,
    normalize_pdf_for_autotag,
    ocr_pdf_for_autotag,
)
from lib.pdf_a11y_audit import audit_pdf_bytes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--diagnostics", required=True)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild an existing structure tree with Adobe Auto-Tag",
    )
    parser.add_argument(
        "--allow-render-change",
        action="store_true",
        help="Write a candidate even when Adobe changes rendered pixels",
    )
    parser.add_argument(
        "--normalize",
        action="store_true",
        help="Rewrite complex input with pypdf before Adobe Auto-Tag",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Rasterize and OCR complex input before Adobe Auto-Tag",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    report_path = Path(args.report)
    diagnostics_path = Path(args.diagnostics)

    original = input_path.read_bytes()
    original_validation = validate_pdf(original)
    original_audit = audit_pdf_bytes(original).to_dict()
    if original_audit["struct_tree_root_present"] and not args.force:
        raise ValueError("Input already has a structure tree; Adobe auto-tag was refused")

    if args.ocr:
        adobe_input = ocr_pdf_for_autotag(original)
    elif args.normalize:
        adobe_input = normalize_pdf_for_autotag(original)
    else:
        adobe_input = original
    tagged, adobe_report = autotag_pdf_from_secret(adobe_input)
    tagged_validation = validate_pdf(tagged)
    tagged_audit = audit_pdf_bytes(tagged).to_dict()
    if not tagged_audit["struct_tree_root_present"] or not tagged_audit["marked"]:
        raise ValueError("Adobe auto-tag returned a PDF without usable tagging")
    if tagged_validation["pages"] != original_validation["pages"]:
        raise ValueError("Adobe auto-tag changed the page count")
    tagged_render_identical = render_hashes(tagged) == render_hashes(original)
    if not tagged_render_identical and not args.allow_render_change:
        raise ValueError("Adobe auto-tag changed rendered page output")

    repaired, sweep = prepare_pdf(tagged)
    repaired_validation = validate_pdf(repaired)
    repaired_audit = audit_pdf_bytes(repaired).to_dict()
    if repaired_validation["pages"] != original_validation["pages"]:
        raise ValueError("Local sweeps changed the page count")
    final_render_identical = render_hashes(repaired) == render_hashes(original)
    if not final_render_identical and not args.allow_render_change:
        raise ValueError("Local sweeps changed rendered page output")
    if not repaired_audit["struct_tree_root_present"] or not repaired_audit["marked"]:
        raise ValueError("Final PDF lost its tagging")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(repaired)
    report_path.write_bytes(adobe_report)
    diagnostics_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "input": str(input_path),
                "output": str(output_path),
                "originalValidation": original_validation,
                "taggedValidation": tagged_validation,
                "finalValidation": repaired_validation,
                "taggedRenderIdentical": tagged_render_identical,
                "finalRenderIdentical": final_render_identical,
                "originalAudit": original_audit,
                "taggedAudit": tagged_audit,
                "finalAudit": repaired_audit,
                "sweep": sweep,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "report": str(report_path),
                "diagnostics": str(diagnostics_path),
                "pages": repaired_validation["pages"],
                "tables": repaired_audit["table_count"],
                "figures": repaired_audit["figure_count"],
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
