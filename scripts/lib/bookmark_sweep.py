"""Repair missing PDF bookmarks from existing heading structure elements."""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field
from numbers import Integral

import pikepdf

from lib.character_encoding_sweep import _decode_mcid_text, _page_fontmaps
from lib.marked_content_actualtext_sweep import (
    _get_mcid_block,
    _page_contents_data,
)


_HEADING_LEVELS = {f"/H{level}": level for level in range(1, 7)}
_MCID_BLOCK_PATTERN = re.compile(
    rb"/[^\s<>{}\[\]()/%]+\s*<<(?:(?!>>).)*?/MCID\s+(\d+)(?!\d)"
    rb"(?:(?!>>).)*>>\s*BDC(?P<body>.*?)(?:EMC|ET)",
    re.DOTALL,
)
_LITERAL_TJ_PATTERN = re.compile(rb"\(((?:\\.|[^\\()])*)\)\s*Tj")


@dataclass
class BookmarkRepairResult:
    pages_found: int
    headings_found: int
    bookmarks_created: int
    headings_skipped: list[str] = field(default_factory=list)
    existing_outline_status: str = "missing"
    existing_outline_preserved: bool = False
    conflicts: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def outline_status(self) -> str:
        """Compatibility alias for callers that use the shorter name."""
        return self.existing_outline_status

    @property
    def skipped_headings(self) -> list[str]:
        """Compatibility alias for callers that use the past-tense name."""
        return self.headings_skipped


@dataclass
class _HeadingCandidate:
    level: int
    page_number: int | None
    label: str | None
    reason: str | None = None


def _object_key(obj: object) -> tuple[int, int] | int:
    objgen = getattr(obj, "objgen", None)
    if isinstance(objgen, tuple) and len(objgen) == 2 and objgen != (0, 0):
        return objgen
    return id(obj)


def _same_object(left: object, right: object) -> bool:
    left_key = getattr(left, "objgen", None)
    right_key = getattr(right, "objgen", None)
    if (
        isinstance(left_key, tuple)
        and isinstance(right_key, tuple)
        and left_key != (0, 0)
        and right_key != (0, 0)
    ):
        return left_key == right_key
    return left is right


def _page_map(
    pdf: pikepdf.Pdf,
) -> tuple[dict[tuple[int, int] | int, pikepdf.Page], dict[tuple[int, int] | int, int]]:
    pages: dict[tuple[int, int] | int, pikepdf.Page] = {}
    numbers: dict[tuple[int, int] | int, int] = {}
    for page_number, page in enumerate(pdf.pages, start=1):
        key = _object_key(page.obj)
        pages[key] = page
        numbers[key] = page_number
    return pages, numbers


def _page_for_reference(
    reference: object,
    pages: dict[tuple[int, int] | int, pikepdf.Page],
) -> pikepdf.Page | None:
    if reference is None:
        return None
    return pages.get(_object_key(reference))


def _is_struct_elem(obj: object) -> bool:
    return isinstance(obj, pikepdf.Dictionary) and (
        str(obj.get("/Type")) == "/StructElem" or obj.get("/S") is not None
    )


def _struct_children(value: object) -> list[pikepdf.Dictionary]:
    if isinstance(value, pikepdf.Dictionary):
        return [value] if _is_struct_elem(value) else []
    if not isinstance(value, pikepdf.Array):
        return []
    children: list[pikepdf.Dictionary] = []
    for item in value:
        if isinstance(item, pikepdf.Dictionary) and _is_struct_elem(item):
            children.append(item)
        elif isinstance(item, pikepdf.Array):
            children.extend(_struct_children(item))
    return children


def _mcid_references(
    value: object,
    *,
    inherited_page: pikepdf.Page | None,
    pages: dict[tuple[int, int] | int, pikepdf.Page],
) -> list[tuple[int, pikepdf.Page | None]]:
    if isinstance(value, Integral):
        return [(int(value), inherited_page)]
    if isinstance(value, pikepdf.Array):
        references: list[tuple[int, pikepdf.Page | None]] = []
        for item in value:
            references.extend(
                _mcid_references(
                    item,
                    inherited_page=inherited_page,
                    pages=pages,
                )
            )
        return references
    if not isinstance(value, pikepdf.Dictionary) or _is_struct_elem(value):
        return []
    mcid = value.get("/MCID")
    if not isinstance(mcid, Integral):
        return []
    page = _page_for_reference(value.get("/Pg"), pages) or inherited_page
    return [(int(mcid), page)]


def _safe_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("<pikepdf.") or "pikepdf.Dictionary" in text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _decode_literal(value: bytes) -> str:
    decoded = bytearray()
    index = 0
    while index < len(value):
        if value[index] != 92:
            decoded.append(value[index])
            index += 1
            continue
        index += 1
        if index >= len(value):
            break
        escaped = value[index]
        simple = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
        if escaped in simple:
            decoded.append(simple[escaped])
            index += 1
        elif 48 <= escaped <= 55:
            digits = [escaped]
            index += 1
            while index < len(value) and len(digits) < 3 and 48 <= value[index] <= 55:
                digits.append(value[index])
                index += 1
            decoded.append(int(bytes(digits), 8))
        else:
            decoded.append(escaped)
            index += 1
    return bytes(decoded).decode("latin1", errors="replace")


def _literal_text(body: bytes) -> str:
    return "".join(_decode_literal(match.group(1)) for match in _LITERAL_TJ_PATTERN.finditer(body))


def _mcid_body(data: bytes, mcid: int) -> bytes | None:
    known_block = _get_mcid_block(data, mcid)
    if known_block is not None:
        return known_block[1]
    token = rb"/MCID\s+" + str(mcid).encode("ascii") + rb"(?!\d)"
    for match in _MCID_BLOCK_PATTERN.finditer(data):
        if re.search(token, match.group(0)):
            return match.group("body")
    return None


def _decoded_mcid_label(page: pikepdf.Page, mcids: list[int]) -> str:
    data = _page_contents_data(page)
    if data is None:
        return ""
    fontmaps = _page_fontmaps(page)
    parts: list[str] = []
    for mcid in mcids:
        body = _mcid_body(data, mcid)
        if body is None:
            continue
        text = _decode_mcid_text(body, fontmaps).strip()
        if not text:
            text = _literal_text(body).strip()
        if text and "\ufffd" not in text:
            parts.append(text)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _heading_label(
    element: pikepdf.Dictionary,
    *,
    page: pikepdf.Page | None,
) -> str:
    for key in ("/T", "/ActualText", "/Alt", "/Contents"):
        label = _safe_text(element.get(key))
        if label:
            return label
    if page is None:
        return ""
    mcid_refs = _mcid_references(element.get("/K"), inherited_page=page, pages={})
    return _decoded_mcid_label(page, [mcid for mcid, _page in mcid_refs])


def _collect_headings(pdf: pikepdf.Pdf) -> tuple[list[_HeadingCandidate], int]:
    pages, page_numbers = _page_map(pdf)
    root = pdf.Root.get("/StructTreeRoot")
    if not isinstance(root, pikepdf.Dictionary):
        return [], 0

    candidates: list[_HeadingCandidate] = []
    headings_found = 0
    seen: set[tuple[int, int] | int] = set()

    def walk(element: pikepdf.Dictionary, inherited_page: pikepdf.Page | None) -> None:
        nonlocal headings_found
        key = _object_key(element)
        if key in seen:
            return
        seen.add(key)
        page = _page_for_reference(element.get("/Pg"), pages) or inherited_page
        level = _HEADING_LEVELS.get(str(element.get("/S")))
        if level is not None:
            headings_found += 1
            mcid_refs = _mcid_references(
                element.get("/K"),
                inherited_page=page,
                pages=pages,
            )
            if page is None and mcid_refs:
                page = next((ref_page for _mcid, ref_page in mcid_refs if ref_page), None)
            label = _heading_label(element, page=page)
            page_number = page_numbers.get(_object_key(page.obj)) if page is not None else None
            reason = None
            if not label:
                reason = "no reliable nonempty label"
            elif page_number is None:
                reason = "no resolved page"
            candidates.append(_HeadingCandidate(level, page_number, label or None, reason))
        for child in _struct_children(element.get("/K")):
            walk(child, page)

    for child in _struct_children(root.get("/K")):
        walk(child, None)
    return candidates, headings_found


def _destination_is_valid(
    item: pikepdf.Dictionary,
    pages: dict[tuple[int, int] | int, pikepdf.Page],
) -> bool:
    destination = item.get("/Dest")
    if isinstance(destination, pikepdf.Array):
        return len(destination) >= 2 and _object_key(destination[0]) in pages
    if isinstance(destination, (pikepdf.Name, pikepdf.String)):
        return bool(str(destination).strip())
    action = item.get("/A")
    return isinstance(action, pikepdf.Dictionary) and action.get("/S") is not None


def _outline_status(
    pdf: pikepdf.Pdf,
    pages: dict[tuple[int, int] | int, pikepdf.Page],
) -> tuple[str, list[str]]:
    raw = pdf.Root.get("/Outlines")
    if raw is None:
        return "missing", []
    if not isinstance(raw, pikepdf.Dictionary):
        return "malformed", ["document /Outlines entry is not a dictionary"]
    first = raw.get("/First")
    last = raw.get("/Last")
    if first is None and last is None:
        count = raw.get("/Count")
        if count is None or (isinstance(count, Integral) and int(count) == 0):
            try:
                pdf.open_outline(strict=True)
            except Exception as error:
                return "malformed", [f"empty outline is malformed: {error}"]
            return "empty", []
        return "malformed", ["outline has a count but no first or last item"]
    if not isinstance(first, pikepdf.Dictionary) or not isinstance(last, pikepdf.Dictionary):
        return "malformed", ["outline first/last links are not dictionaries"]
    try:
        outline = pdf.open_outline(strict=True)
        if not outline.root:
            return "malformed", ["outline links contain no readable items"]
    except Exception as error:
        return "malformed", [f"existing outline failed strict validation: {error}"]

    conflicts: list[str] = []
    visited: set[tuple[int, int] | int] = set()
    reachable_last: set[tuple[int, int] | int] = set()

    def walk_siblings(
        first_item: object,
        expected_parent: pikepdf.Dictionary,
        expected_last: object,
    ) -> None:
        current = first_item
        previous: pikepdf.Dictionary | None = None
        while current is not None:
            if not isinstance(current, pikepdf.Dictionary):
                conflicts.append("outline sibling link is not a dictionary")
                return
            key = _object_key(current)
            if key in visited:
                conflicts.append("outline contains a cyclic sibling or child link")
                return
            visited.add(key)
            if not _same_object(current.get("/Parent"), expected_parent):
                conflicts.append("outline item has an incorrect /Parent link")
            if previous is None:
                if current.get("/Prev") is not None:
                    conflicts.append("outline first item has an unexpected /Prev link")
            elif not _same_object(current.get("/Prev"), previous):
                conflicts.append("outline /Prev and /Next links conflict")
            if not _destination_is_valid(current, pages):
                conflicts.append("outline item has no valid destination or action")
            child_first = current.get("/First")
            child_last = current.get("/Last")
            if (child_first is None) != (child_last is None):
                conflicts.append("outline item has only one of /First and /Last")
            if child_first is not None and child_last is not None:
                if not isinstance(child_first, pikepdf.Dictionary) or not isinstance(
                    child_last, pikepdf.Dictionary
                ):
                    conflicts.append("outline child first/last link is not a dictionary")
                else:
                    walk_siblings(child_first, current, child_last)
            next_item = current.get("/Next")
            if next_item is None and not _same_object(current, expected_last):
                conflicts.append("outline /Last does not match the sibling chain")
                return
            if _same_object(current, expected_last):
                reachable_last.add(_object_key(current))
            previous = current
            current = next_item

    walk_siblings(first, raw, last)
    if _object_key(last) not in reachable_last:
        conflicts.append("outline /Last item is not reachable from /First")
    return ("malformed", conflicts) if conflicts else ("valid", [])


def repair_bookmarks(pdf_bytes: bytes) -> tuple[bytes, BookmarkRepairResult]:
    """Create bookmarks from existing H1-H6 structure elements when needed."""
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        pages, _page_numbers = _page_map(pdf)
        status, conflicts = _outline_status(pdf, pages)
        candidates, headings_found = _collect_headings(pdf)
        pages_found = len(pdf.pages)
        if status == "valid":
            return pdf_bytes, BookmarkRepairResult(
                pages_found=pages_found,
                headings_found=headings_found,
                bookmarks_created=0,
                existing_outline_status=status,
                existing_outline_preserved=True,
                conflicts=conflicts,
                actions=[],
            )
        if status == "malformed":
            return pdf_bytes, BookmarkRepairResult(
                pages_found=pages_found,
                headings_found=headings_found,
                bookmarks_created=0,
                existing_outline_status=status,
                conflicts=conflicts,
                actions=[],
            )

        skipped: list[str] = []
        usable = []
        for index, candidate in enumerate(candidates, start=1):
            if candidate.reason is not None:
                skipped.append(f"heading {index}: {candidate.reason}")
            else:
                usable.append(candidate)
        if not usable:
            return pdf_bytes, BookmarkRepairResult(
                pages_found=pages_found,
                headings_found=headings_found,
                bookmarks_created=0,
                headings_skipped=skipped,
                existing_outline_status=status,
                conflicts=conflicts,
                actions=[],
            )

        actions: list[str] = []
        with pdf.open_outline() as outline:
            stack: list[tuple[int, pikepdf.OutlineItem]] = []
            for candidate in usable:
                item = pikepdf.OutlineItem(
                    candidate.label or "",
                    destination=(candidate.page_number or 1) - 1,
                    page_location="Fit",
                )
                while stack and stack[-1][0] >= candidate.level:
                    stack.pop()
                if stack:
                    stack[-1][1].children.append(item)
                else:
                    outline.root.append(item)
                stack.append((candidate.level, item))
                actions.append(
                    f"heading H{candidate.level}: bookmark {candidate.label!r} "
                    f"to page {candidate.page_number}"
                )
        pdf.Root["/PageMode"] = pikepdf.Name("/UseOutlines")
        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue(), BookmarkRepairResult(
            pages_found=pages_found,
            headings_found=headings_found,
            bookmarks_created=len(usable),
            headings_skipped=skipped,
            existing_outline_status=status,
            conflicts=conflicts,
            actions=actions,
        )
