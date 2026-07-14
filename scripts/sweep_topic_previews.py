#!/usr/bin/env python3
"""Re-apply post-remediation sweeps to topic preview PDFs (no Adobe pass).

Use after changing sweep code, or to validate previews before uploading to S3.
See docs/PREVIEW_SCOPE.md — this tool only touches topic_pdfs/{topicId}.pdf.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pikepdf

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from lib.channels_paths import find_chapter, load_course_json, topics_for_chapter  # noqa: E402
from lib.character_encoding_sweep import (  # noqa: E402
    _decode_mcid_text,
    _needs_character_encoding_repair,
    _page_fontmaps,
    repair_character_encoding,
)
from lib.cloudfront import invalidate_paths  # noqa: E402
from lib.config import channels_bucket, cloudfront_distribution_id  # noqa: E402
from lib.figure_alt_sweep import repair_missing_figure_alt  # noqa: E402
from lib.inline_formula_sweep import repair_inline_formula_figures  # noqa: E402
from lib.layout_table_sweep import repair_layout_tables  # noqa: E402
from lib.marked_content_actualtext_sweep import (  # noqa: E402
    _get_mcid_block,
    _mcid_bdc_has_actualtext,
    _read_page_contents,
    count_li_lbl_missing_actualtext,
    count_orphan_marked_missing_actualtext,
    repair_marked_content_actualtext,
)
from lib.pdf_a11y_audit import audit_pdf_bytes  # noqa: E402
from migrate_channels_worksheets import filter_blocking_suspicious_figure_alts  # noqa: E402


@dataclass
class PreviewAuditFlags:
    topic_id: str
    title: str
    missing_figure_alt: int
    suspicious_figure_alt: int
    lbl_blank_without_actualtext: int
    lbl_mcq_without_actualtext: int
    orphan_marked_without_actualtext: int
    symbol_without_actualtext: int
    status: str
    notes: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Post-remediation sweeps on topic preview PDFs only (no Adobe)"
    )
    parser.add_argument("--course-id", required=True)
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    parser.add_argument("--toc-id", default="bcddb411")
    parser.add_argument("--chapter-id", required=True, help="Chapter to process")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Local dir for before/swept PDFs (default: tmp/{chapter-id}-previews-a11y)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload swept PDFs to S3 and invalidate CDN (default: local only)",
    )
    parser.add_argument("--skip-cdn-invalidation", action="store_true")
    return parser.parse_args()


def apply_sweeps(pdf_bytes: bytes) -> tuple[bytes, dict]:
    remediated = pdf_bytes
    remediated, repaired_figures = repair_missing_figure_alt(remediated)
    remediated, inline = repair_inline_formula_figures(remediated)
    remediated, tables = repair_layout_tables(remediated)
    remediated, marked = repair_marked_content_actualtext(remediated)
    remediated, encoding = repair_character_encoding(remediated)
    return remediated, {
        "repairedFigures": repaired_figures,
        "inlineFormulaRepair": inline.to_dict() if inline else None,
        "tableRepair": tables.to_dict() if tables else None,
        "markedContentRepair": marked.to_dict() if marked else None,
        "characterEncodingRepair": encoding.to_dict() if encoding else None,
    }


def audit_preview_bytes(topic_id: str, title: str, pdf_bytes: bytes) -> PreviewAuditFlags:
    audit = audit_pdf_bytes(pdf_bytes)
    notes: list[str] = []
    lbl_bad = 0
    lbl_mcq_bad = count_li_lbl_missing_actualtext(pdf_bytes)
    orphan_bad = count_orphan_marked_missing_actualtext(pdf_bytes)
    sym_bad = 0

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            data = _read_page_contents(page.get("/Contents"))
            if not data:
                continue
            fontmaps = _page_fontmaps(page)
            for mcid in range(1, 250):
                block = _get_mcid_block(data, mcid)
                if block is None:
                    continue
                tag, body = block
                text = _decode_mcid_text(body, fontmaps)
                if tag == "Lbl" and b"<0191" in body and not _mcid_bdc_has_actualtext(
                    data, mcid
                ):
                    lbl_bad += 1
                if _needs_character_encoding_repair(text) and not _mcid_bdc_has_actualtext(
                    data, mcid
                ):
                    if any(char in text for char in "□●αβ"):
                        sym_bad += 1

    blocking = filter_blocking_suspicious_figure_alts(
        audit.figures_suspicious_alt,
        marked_content_actions=None,
    )
    status = "ok"
    if audit.figures_missing_alt:
        status = "warn-missing-alt"
        notes.append(f"missing figure alt: {audit.figures_missing_alt}")
    elif blocking:
        status = "warn-suspicious-alt"
        notes.append(f"suspicious alt figures: {[i.figure_index for i in blocking]}")
    elif lbl_bad or lbl_mcq_bad or orphan_bad or sym_bad:
        status = "warn-encoding"
        if lbl_bad:
            notes.append(f"lbl blank without ActualText: {lbl_bad}")
        if lbl_mcq_bad:
            notes.append(f"lbl mcq without ActualText: {lbl_mcq_bad}")
        if orphan_bad:
            notes.append(f"orphan marked without ActualText: {orphan_bad}")
        if sym_bad:
            notes.append(f"symbol without ActualText: {sym_bad}")

    return PreviewAuditFlags(
        topic_id=topic_id,
        title=title,
        missing_figure_alt=len(audit.figures_missing_alt),
        suspicious_figure_alt=len(blocking),
        lbl_blank_without_actualtext=lbl_bad,
        lbl_mcq_without_actualtext=lbl_mcq_bad,
        orphan_marked_without_actualtext=orphan_bad,
        symbol_without_actualtext=sym_bad,
        status=status,
        notes=notes,
    )


def main() -> int:
    args = parse_args()
    bucket = channels_bucket(args.env)
    out_dir = Path(
        args.output_dir or f"tmp/{args.chapter_id}-previews-a11y"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    s3 = boto3.client("s3")
    course = load_course_json(s3, bucket, args.course_id)
    _toc_id, chapter = find_chapter(course, args.chapter_id, args.toc_id)
    topics = topics_for_chapter(s3, bucket, args.course_id, course, chapter)

    print(f"Chapter: {chapter.get('title')} ({args.chapter_id})")
    print(f"Topics: {len(topics)}")
    print(f"Output: {out_dir.resolve()}")
    print(f"Upload: {'yes' if args.upload else 'no (local only)'}")

    results: list[dict] = []
    uploaded_paths: list[str] = []
    failed: list[dict] = []

    for topic in topics:
        print(f"\n  {topic.topic_id} — {topic.title}")
        try:
            before = s3.get_object(Bucket=bucket, Key=topic.preview_key)["Body"].read()
            (out_dir / f"{topic.topic_id}-before.pdf").write_bytes(before)
            swept, repairs = apply_sweeps(before)
            (out_dir / f"{topic.topic_id}-swept.pdf").write_bytes(swept)
            flags = audit_preview_bytes(topic.topic_id, topic.title, swept)
            entry = {
                **flags.to_dict(),
                "previewKey": topic.preview_key,
                "repairs": repairs,
            }
            results.append(entry)
            print(f"    {flags.status}" + (f" — {flags.notes}" if flags.notes else ""))

            if args.upload:
                s3.put_object(
                    Bucket=bucket,
                    Key=topic.preview_key,
                    Body=swept,
                    ContentType="application/pdf",
                )
                uploaded_paths.append(f"/{topic.preview_key}")
                print("    uploaded")
        except Exception as error:
            print(f"    FAILED: {error}")
            failed.append(
                {"topicId": topic.topic_id, "title": topic.title, "error": str(error)}
            )

    summary = {
        "runAt": datetime.now(timezone.utc).isoformat(),
        "courseId": args.course_id,
        "chapterId": args.chapter_id,
        "chapterTitle": chapter.get("title"),
        "topicCount": len(topics),
        "uploaded": len(uploaded_paths),
        "results": results,
        "failed": failed,
    }
    if uploaded_paths and not args.skip_cdn_invalidation:
        inv = invalidate_paths(cloudfront_distribution_id(args.env), uploaded_paths)
        summary["cdnInvalidation"] = inv
        print(f"\nCDN invalidation: {inv}")

    (out_dir / "sweep-summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nSummary: {out_dir / 'sweep-summary.json'}")

    ok = sum(1 for r in results if r["status"] == "ok")
    warn = len(results) - ok
    print(f"Done: {ok} ok, {warn} warnings, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
