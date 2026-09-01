#!/usr/bin/env python3

import argparse
import json
from pathlib import Path

from lib.accessibility_course_workflow import prepare_pdf


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply local accessibility sweeps to one PDF"
    )
    parser.add_argument("input")
    parser.add_argument("--output", required=True)
    parser.add_argument("--diagnostics", required=True)
    args = parser.parse_args()

    repaired, diagnostics = prepare_pdf(Path(args.input).read_bytes())
    if not diagnostics["renderValidation"]["identical"]:
        raise ValueError("Local sweeps changed rendered page output")

    output_path = Path(args.output)
    diagnostics_path = Path(args.diagnostics)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(repaired)
    diagnostics_path.write_text(
        json.dumps(diagnostics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "diagnostics": str(diagnostics_path),
                "appliedRepairs": diagnostics["appliedRepairs"],
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
