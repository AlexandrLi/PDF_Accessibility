"""Heuristics for figure /Alt text quality in remediated preview PDFs."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pikepdf

# Ends with a list/sequence introducer and a lone digit (e.g. "...four stages: 1").
_TRUNCATED_LIST_ITEM = re.compile(
    r"(?:stages?|steps?|phases?|parts?|points?|items?|types?|examples?|diagrams?)"
    r"\s*:\s*\d+\s*$",
    re.IGNORECASE,
)

# Ends with ": N" where N is 1–9 and the alt does not look complete.
_TRUNCATED_COLON_DIGIT = re.compile(r":\s*\d+\s*$")

# Caption-only alt on inline-formula figures (title line, not equation description).
_INLINE_FORMULA_CAPTION_MAX_LEN = 80

# Alt text that describes tabular data on a /Figure (should be /Table).
_TABLE_AS_FIGURE_PREFIX = re.compile(
    r"^(?:a )?table(?: with| showing| describing| of)\b",
    re.IGNORECASE,
)
_TABLE_COLUMN_ROW_PATTERN = re.compile(
    r"columns?(?: are)?(?: labeled| labelled| include)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SuspiciousFigureAlt:
    figure_index: int
    alt_text: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "figureIndex": self.figure_index,
            "altPreview": self.alt_text[:120],
            "reasons": list(self.reasons),
        }


def struct_class_names(struct_elem: object) -> set[str]:
    """Return Adobe style class names from a struct element /C entry."""
    if not hasattr(struct_elem, "get"):
        return set()
    raw = struct_elem.get("/C")
    if raw is None:
        return set()

    names: set[str] = set()

    def add_name(value: object) -> None:
        if value is None:
            return
        if isinstance(value, pikepdf.Dictionary):
            nested = value.get("/N")
            if nested is not None:
                names.add(str(nested))
            return
        text = str(value).strip()
        if text:
            names.add(text)

    if isinstance(raw, list) or (
        hasattr(raw, "__iter__") and not isinstance(raw, (str, bytes))
    ):
        try:
            for item in raw:
                add_name(item)
        except TypeError:
            add_name(raw)
    else:
        add_name(raw)

    return names


def classify_figure_alt(
    alt_text: str,
    *,
    struct_classes: set[str] | None = None,
) -> list[str]:
    """Return human-readable reasons when figure alt text looks incomplete or wrong."""
    text = alt_text.strip()
    if not text:
        return []

    reasons: list[str] = []
    classes = struct_classes or set()

    if _TRUNCATED_LIST_ITEM.search(text):
        reasons.append("truncated_list_enumeration")
    elif _TRUNCATED_COLON_DIGIT.search(text) and len(text) < 200:
        reasons.append("truncated_colon_digit")

    if "inlineFormula" in classes or any("inlineFormula" in name for name in classes):
        if len(text) < _INLINE_FORMULA_CAPTION_MAX_LEN and not _looks_like_equation_alt(text):
            reasons.append("inline_formula_caption_only")

    if looks_like_table_figure_alt(text) and not _is_comprehensive_table_alt(text):
        if not any("table-figure-reverted" in name for name in classes):
            reasons.append("table_tagged_as_figure")

    return reasons


def looks_like_equation_alt(text: str) -> bool:
    return _looks_like_equation_alt(text)


def _looks_like_equation_alt(text: str) -> bool:
    lowered = text.lower()
    equation_markers = (
        "equals",
        "subscript",
        "superscript",
        "open parenthesis",
        "close parenthesis",
        "to the power of",
        "divided by",
    )
    return any(marker in lowered for marker in equation_markers)


def _is_comprehensive_table_alt(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if _TABLE_COLUMN_ROW_PATTERN.search(stripped) and len(stripped) >= 150:
        return True
    if _TABLE_AS_FIGURE_PREFIX.search(stripped) and len(stripped) >= 200:
        return True
    return len(stripped) >= 200 and bool(_TABLE_COLUMN_ROW_PATTERN.search(stripped))


def looks_like_table_figure_alt(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return bool(
        _TABLE_AS_FIGURE_PREFIX.search(stripped)
        or (
            _TABLE_COLUMN_ROW_PATTERN.search(stripped)
            and re.search(r"\brows?\b", stripped, re.IGNORECASE)
        )
    )


def is_suspicious_figure_alt(
    alt_text: str,
    *,
    struct_classes: set[str] | None = None,
) -> bool:
    return bool(classify_figure_alt(alt_text, struct_classes=struct_classes))
