"""Reusable local PDF preparation for accessibility course workflows."""

from __future__ import annotations

import hashlib
import io
from dataclasses import asdict, is_dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

import pikepdf
import pymupdf

from lib.bookmark_sweep import repair_bookmarks
from lib.character_encoding_sweep import repair_character_encoding
from lib.document_title_sweep import repair_document_title
from lib.figure_alt_sweep import repair_missing_figure_alt
from lib.heading_nesting_sweep import repair_heading_nesting
from lib.inline_formula_sweep import repair_inline_formula_figures
from lib.layout_table_sweep import repair_layout_tables
from lib.marked_content_actualtext_sweep import repair_marked_content_actualtext
from lib.pdf_a11y_audit import audit_pdf_bytes
from lib.tab_order_sweep import repair_tab_order
from lib.tagged_annotation_sweep import repair_tagged_annotations
from lib.tagged_content_sweep import repair_tagged_content


SWEEP_MODULES = (
    "bookmark_sweep.py",
    "character_encoding_sweep.py",
    "document_title_sweep.py",
    "figure_alt_sweep.py",
    "heading_nesting_sweep.py",
    "inline_formula_sweep.py",
    "layout_table_sweep.py",
    "marked_content_actualtext_sweep.py",
    "pdf_a11y_audit.py",
    "tab_order_sweep.py",
    "tagged_annotation_sweep.py",
    "tagged_content_sweep.py",
)


def serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    return str(value)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def pipeline_fingerprint() -> str:
    digest = hashlib.sha256()
    module_dir = Path(__file__).resolve().parent
    for name in (Path(__file__).name, *SWEEP_MODULES):
        path = module_dir / name
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def validate_pdf(pdf_bytes: bytes) -> dict[str, Any]:
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        return {
            "open": True,
            "pages": len(pdf.pages),
            "bytes": len(pdf_bytes),
            "sha256": sha256_bytes(pdf_bytes),
        }


def render_hashes(pdf_bytes: bytes) -> list[str]:
    document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [
            sha256_bytes(
                page.get_pixmap(
                    matrix=pymupdf.Matrix(1, 1),
                    alpha=False,
                ).samples
            )
            for page in document
        ]
    finally:
        document.close()


def run_sweeps(
    pdf_bytes: bytes,
    *,
    document_title: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    current = pdf_bytes
    repairs: dict[str, Any] = {}
    warnings: list[str] = []
    applied: list[str] = []
    timings: dict[str, dict[str, Any]] = {}
    audit_before: dict[str, Any] | None = None

    def run_stage(
        name: str,
        repair: Callable[[bytes], tuple[bytes, Any]],
    ) -> Any | None:
        nonlocal current
        before = current
        started = perf_counter()
        try:
            current, result = repair(current)
            repairs[name] = serialize(result)
            changed = current != before
            if changed:
                applied.append(name)
            timings[name] = {
                "durationMs": round((perf_counter() - started) * 1000, 3),
                "inputBytes": len(before),
                "outputBytes": len(current),
                "changed": changed,
                "error": None,
            }
            return result
        except Exception as error:
            current = before
            repairs[name] = None
            warnings.append(f"{name} failed: {error}")
            timings[name] = {
                "durationMs": round((perf_counter() - started) * 1000, 3),
                "inputBytes": len(before),
                "outputBytes": len(before),
                "changed": False,
                "error": str(error),
            }
            return None

    run_stage("tabOrder", repair_tab_order)
    run_stage("taggedContent", repair_tagged_content)
    run_stage("taggedAnnotations", repair_tagged_annotations)

    started = perf_counter()
    try:
        audit_before = audit_pdf_bytes(current).to_dict()
        repairs["auditBeforeRepairs"] = audit_before
        audit_error = None
    except Exception as error:
        warnings.append(f"audit before repairs failed: {error}")
        repairs["auditBeforeRepairs"] = None
        audit_error = str(error)
    timings["auditBeforeRepairs"] = {
        "durationMs": round((perf_counter() - started) * 1000, 3),
        "inputBytes": len(current),
        "outputBytes": len(current),
        "changed": False,
        "error": audit_error,
    }

    if audit_before and audit_before.get("figures_missing_alt"):
        run_stage("figureAlt", repair_missing_figure_alt)
    else:
        repairs["figureAlt"] = {"skipped": "no missing figure alt reported"}
        timings["figureAlt"] = {
            "durationMs": 0.0,
            "inputBytes": len(current),
            "outputBytes": len(current),
            "changed": False,
            "error": None,
        }

    run_stage("inlineFormula", repair_inline_formula_figures)
    run_stage("layoutTable", repair_layout_tables)
    run_stage("markedContentActualText", repair_marked_content_actualtext)
    run_stage("characterEncoding", repair_character_encoding)
    run_stage("headingNesting", repair_heading_nesting)
    run_stage("bookmarks", repair_bookmarks)
    run_stage(
        "documentTitle",
        lambda data: repair_document_title(data, title=document_title),
    )

    started = perf_counter()
    try:
        repairs["auditAfterRepairs"] = audit_pdf_bytes(current).to_dict()
        audit_error = None
    except Exception as error:
        repairs["auditAfterRepairs"] = None
        warnings.append(f"audit after repairs failed: {error}")
        audit_error = str(error)
    timings["auditAfterRepairs"] = {
        "durationMs": round((perf_counter() - started) * 1000, 3),
        "inputBytes": len(current),
        "outputBytes": len(current),
        "changed": False,
        "error": audit_error,
    }

    return current, {
        "appliedRepairs": applied,
        "repairs": repairs,
        "warnings": warnings,
        "stageTelemetry": timings,
    }


def prepare_pdf(
    pdf_bytes: bytes,
    *,
    document_title: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    original_validation = validate_pdf(pdf_bytes)
    original_render_hashes = render_hashes(pdf_bytes)
    swept_bytes, sweep_result = run_sweeps(
        pdf_bytes,
        document_title=document_title,
    )
    swept_validation = validate_pdf(swept_bytes)
    swept_render_hashes = render_hashes(swept_bytes)
    changed = swept_bytes != pdf_bytes

    if changed:
        second_bytes, second_result = run_sweeps(
            swept_bytes,
            document_title=document_title,
        )
        second_pass = {
            "executed": True,
            "byteStable": second_bytes == swept_bytes,
            "appliedRepairs": second_result["appliedRepairs"],
            "warnings": second_result["warnings"],
        }
        if second_bytes != swept_bytes:
            sweep_result["warnings"].append(
                "second pass was not byte-stable; first-pass output preserved"
            )
    else:
        second_pass = {
            "executed": False,
            "byteStable": True,
            "appliedRepairs": [],
            "warnings": [],
            "reason": "first pass was byte-identical",
        }

    sweep_result.update(
        {
            "originalValidation": original_validation,
            "reSweptValidation": swept_validation,
            "renderValidation": {
                "identical": original_render_hashes == swept_render_hashes,
                "originalPageHashes": original_render_hashes,
                "reSweptPageHashes": swept_render_hashes,
            },
            "secondPass": second_pass,
        }
    )
    if not sweep_result["renderValidation"]["identical"]:
        sweep_result["warnings"].append("rendered page hashes changed")
    return swept_bytes, sweep_result
