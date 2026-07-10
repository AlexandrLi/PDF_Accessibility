"""Expand caption-only alt text on inline-formula /Figure elements."""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass

import pikepdf

from lib.figure_alt_quality import (
    classify_figure_alt,
    looks_like_equation_alt,
    struct_class_names,
)


@dataclass
class InlineFormulaRepairResult:
    figures_found: int
    converted: int
    actions: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


_ANTIPORTER_PATTERN = re.compile(
    r"^(?P<prefix>.*?)\s+"
    r"(?P<left>[A-Za-z0-9+\-]+)-/(?P<right>[A-Za-z0-9+\-]+)-\s+"
    r"(?P<transporter>Antiporter|Symporter|Uniporter|Transporter|Pump|Porter)\.?$",
    re.IGNORECASE,
)


def _chem_spoken(token: str) -> str:
    spoken = token.strip().rstrip("-")
    replacements = (
        (r"^Cl$", "chloride, Cl"),
        (r"^HCO3$", "bicarbonate, H C O 3"),
        (r"^Na$", "sodium, N a"),
        (r"^K$", "potassium, K"),
        (r"^Ca$", "calcium, C a"),
        (r"^Glucose$", "glucose"),
    )
    for pattern, phrase in replacements:
        if re.fullmatch(pattern, spoken, re.IGNORECASE):
            return phrase
    return spoken


def expand_inline_formula_alt(caption: str) -> str:
    """Turn a short inline-formula caption into spoken equation-style alt text."""
    text = caption.strip()
    if not text:
        return text
    if looks_like_equation_alt(text) and len(text) >= 40:
        return text

    match = _ANTIPORTER_PATTERN.match(text)
    if match:
        prefix = match.group("prefix").strip()
        left = match.group("left")
        right = match.group("right")
        transporter = match.group("transporter")
        left_spoken = _chem_spoken(left)
        right_spoken = _chem_spoken(right)
        prefix_clause = f"{prefix} " if prefix else ""
        return (
            f"{prefix_clause}{transporter} exchanging {left_spoken} minus and "
            f"{right_spoken} minus, written as {left} minus divided by {right} minus."
        )

    expanded = text
    token_rules = (
        (r"Cl-", "chloride, Cl minus"),
        (r"HCO3-", "bicarbonate, H C O 3 minus"),
        (r"Na\+", "sodium, N a plus"),
        (r"K\+", "potassium, K plus"),
        (r"Ca2\+", "calcium, C a 2 plus"),
        (r"/", " divided by "),
        (r"-", " minus"),
        (r"\+", " plus"),
    )
    for pattern, replacement in token_rules:
        expanded = re.sub(pattern, replacement, expanded)

    if not looks_like_equation_alt(expanded):
        expanded = f"{expanded}. Spoken formula notation for inline chemistry text."

    return re.sub(r"\s+", " ", expanded).strip()


def _expand_inline_formula_figure(
    figure: pikepdf.Dictionary,
    *,
    figure_index: int,
    alt_text: str,
) -> str:
    spoken_alt = expand_inline_formula_alt(alt_text)
    figure["/Alt"] = pikepdf.String(spoken_alt)
    figure["/Contents"] = pikepdf.String(spoken_alt)
    return f"figure{figure_index}: expanded inline formula alt on /Figure"


def repair_inline_formula_figures(pdf_bytes: bytes) -> tuple[bytes, InlineFormulaRepairResult]:
    figures_found = 0
    converted = 0
    actions: list[str] = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            result = InlineFormulaRepairResult(0, 0, [])
            return pdf_bytes, result

        figure_index = 0

        def walk(obj: pikepdf.Dictionary) -> None:
            nonlocal figure_index, figures_found, converted
            if obj.get("/S") == "/Figure":
                figure_index += 1
                alt = obj.get("/Alt")
                alt_text = str(alt).strip() if alt is not None else ""
                reasons = classify_figure_alt(
                    alt_text,
                    struct_classes=struct_class_names(obj),
                )
                if "inline_formula_caption_only" in reasons:
                    figures_found += 1
                    action = _expand_inline_formula_figure(
                        obj,
                        figure_index=figure_index,
                        alt_text=alt_text,
                    )
                    converted += 1
                    actions.append(action)

            kids = obj.get("/K")
            if isinstance(kids, pikepdf.Array):
                for kid in kids:
                    if isinstance(kid, pikepdf.Dictionary):
                        walk(kid)
                    elif isinstance(kid, pikepdf.Array):
                        for nested in kid:
                            if isinstance(nested, pikepdf.Dictionary):
                                walk(nested)
            elif isinstance(kids, pikepdf.Dictionary):
                walk(kids)

        walk(struct_root)

        output = io.BytesIO()
        pdf.save(output)
        result = InlineFormulaRepairResult(
            figures_found=figures_found,
            converted=converted,
            actions=actions,
        )
        return output.getvalue(), result
