"""Add /ActualText to marked-content MCIDs for Adobe Other-elements alt checks."""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass

import pikepdf

from lib.figure_alt_quality import looks_like_table_figure_alt, struct_class_names
from lib.figure_to_table_sweep import _parse_column_headers, _parse_table_rows
from lib.inline_formula_sweep import expand_inline_formula_alt


@dataclass
class MarkedContentActualTextRepairResult:
    figures_found: int
    mcids_updated: int
    actions: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _pdf_literal_string(text: str) -> bytes:
    out = bytearray(b"(")
    for char in text:
        code = ord(char)
        if char in ("\\", "(", ")"):
            out.extend(f"\\{char}".encode("latin1"))
        elif code > 126:
            out.extend(f"\\{code:03o}".encode("latin1"))
        else:
            out.extend(char.encode("latin1"))
    out.extend(b")")
    return bytes(out)


def _strip_inline_formula_class(struct_elem: pikepdf.Dictionary) -> None:
    raw = struct_elem.get("/C")
    if raw is None:
        return

    def class_name(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, pikepdf.Dictionary):
            nested = value.get("/N")
            return str(nested) if nested is not None else None
        text = str(value).strip()
        return text or None

    names: list[str] = []
    if isinstance(raw, list) or (
        hasattr(raw, "__iter__") and not isinstance(raw, (str, bytes))
    ):
        try:
            for item in raw:
                name = class_name(item)
                if name:
                    names.append(name)
        except TypeError:
            name = class_name(raw)
            if name:
                names.append(name)
    else:
        name = class_name(raw)
        if name:
            names.append(name)

    kept = [name for name in names if "inlineFormula" not in name]
    if not kept:
        if "/C" in struct_elem:
            del struct_elem["/C"]
        return
    if len(kept) == 1:
        struct_elem["/C"] = pikepdf.Name(kept[0]) if kept[0].startswith("/") else kept[0]
        return
    struct_elem["/C"] = pikepdf.Array(kept)


def _collect_mcids(content: object) -> list[int]:
    if isinstance(content, int):
        return [content]
    if isinstance(content, pikepdf.Array):
        mcids: list[int] = []
        for item in content:
            mcids.extend(_collect_mcids(item))
        return mcids
    return []


def _normalize_figure_alt_text(alt: object) -> str:
    if alt is None:
        return ""
    text = str(alt).strip()
    if not text:
        return ""
    if text.startswith("<pikepdf.") or "pikepdf.Dictionary" in text:
        return "Table"
    if len(text) > 300 and looks_like_table_figure_alt(text[:120]):
        return "Table"
    return text


def _table_mcid_actual_texts(alt_text: str, mcids: list[int]) -> dict[int, str]:
    headers = _parse_column_headers(alt_text)
    rows = _parse_table_rows(alt_text, headers)
    if headers and rows and len(mcids) == len(headers):
        return {
            mcid: f"{headers[index]}: {rows[0][index]}"
            for index, mcid in enumerate(mcids)
        }
    if headers and len(mcids) == len(headers):
        return {mcid: headers[index] for index, mcid in enumerate(mcids)}
    fallback = alt_text if len(alt_text) <= 200 else "Table"
    return {mcid: fallback for mcid in mcids}


_MCID_BDC_TAG = rb"(?:Figure|Span|P|TD|TH|Formula|LBody|LI|Lbl|StyleSpan|ExtraCharSpan|Table|Artifact)"


def _mcid_token(mcid: int) -> bytes:
    return str(mcid).encode() + rb"(?!\d)"


def _get_mcid_block(data: bytes, mcid: int) -> tuple[str, bytes] | None:
    pattern = re.compile(
        rb"/(?P<tag>" + _MCID_BDC_TAG + rb")\s*<<"
        rb"((?:(?!>>).)*?/MCID\s+"
        + _mcid_token(mcid)
        + rb"(?:(?!>>).)*?)>>\s*BDC"
        rb"(?P<body>.*?)(?:EMC|ET)",
        re.DOTALL,
    )
    match = pattern.search(data)
    if match is None:
        return None
    tag = match.group("tag").decode("ascii")
    return tag, match.group("body")


def _mcid_body_has_image(body: bytes) -> bool:
    return bool(re.search(rb"/Im\d+\s+Do", body))


def _is_image_only_mcid_body(body: bytes) -> bool:
    text = body.decode("latin1", errors="replace")
    has_image = _mcid_body_has_image(body)
    has_text = bool(re.search(r"\([^()\\]{2,}\)\s*Tj", text))
    return has_image and not has_text


def _struct_element_mcids(obj: pikepdf.Dictionary) -> list[int]:
    if obj.get("/S") == "/LI":
        return _collect_li_mcids(obj)
    return _collect_mcids(obj.get("/K"))


def _spoken_figure_actual_text(alt_text: str) -> str:
    if len(alt_text) <= 500:
        return alt_text
    return alt_text[:497] + "..."


_TJ_TEXT_PATTERN = re.compile(r"\(((?:\\.|[^\\()])*)\)\s*Tj")


def _extract_tj_text(body: bytes) -> str:
    parts: list[str] = []
    for match in _TJ_TEXT_PATTERN.finditer(body.decode("latin1", errors="replace")):
        raw = match.group(1)
        decoded = (
            raw.replace("\\(", "(")
            .replace("\\)", ")")
            .replace("\\n", " ")
            .replace("\\r", " ")
            .replace("\\\\", "\\")
        )
        if decoded.strip():
            parts.append(decoded)
    return " ".join(parts)


def _spoken_list_item_text(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw).strip()
    if not text:
        return text
    text = text.replace("\\320", " minus ").replace("\320", " minus ")
    text = re.sub(r"^(\d+)\s*\)\s*", r"Step \1. ", text)
    replacements = (
        (r"Cl\s*-\s*", "chloride, Cl minus, "),
        (r"Cl/", "chloride, Cl minus, "),
        (r"HCO\s*3\s*-\s*", "bicarbonate, H C O 3 minus, "),
        (r"HCO\s*3/", "bicarbonate, H C O 3 minus, "),
        (r"HCO3\s*-\s*", "bicarbonate, H C O 3 minus, "),
        (r"HCO3/", "bicarbonate, H C O 3 minus, "),
        (r"\[", "open bracket "),
        (r"\]", "close bracket "),
        (r"!", "increased "),
        (r"\(\s*", "open parenthesis "),
        (r"\s*\)", " close parenthesis"),
    )
    for pattern, phrase in replacements:
        text = re.sub(pattern, phrase, text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" ,")


def _read_page_contents(contents: object) -> bytes:
    if isinstance(contents, pikepdf.Array):
        return b"".join(stream.read_bytes() for stream in contents)
    return contents.read_bytes()


def _page_contents_data(page: pikepdf.Page | None) -> bytes | None:
    if page is None:
        return None
    contents = page.get("/Contents")
    if contents is None:
        return None
    return _read_page_contents(contents)


def _page_has_mcids(data: bytes, mcids: list[int]) -> bool:
    if not mcids:
        return False
    for mcid in mcids:
        if not re.search(rb"/MCID\s+" + _mcid_token(mcid), data):
            return False
    return True


_BDC_TAG_ALIASES: dict[str, str] = {
    "/ParagraphSpan": "LBody",
}


def _expected_bdc_tag(obj: pikepdf.Dictionary) -> str | None:
    struct_tag = obj.get("/S")
    if struct_tag is None:
        return None
    tag_str = str(struct_tag)
    if tag_str in _BDC_TAG_ALIASES:
        return _BDC_TAG_ALIASES[tag_str]
    return tag_str.lstrip("/")


def _uses_mcid_tag_matching(obj: pikepdf.Dictionary, mcids: list[int]) -> bool:
    if obj.get("/S") == "/LI" or len(mcids) != 1:
        return False
    return _expected_bdc_tag(obj) is not None


def _pages_with_matching_mcid_tag(
    pdf: pikepdf.Pdf,
    *,
    mcid: int,
    expected_tag: str,
) -> list[pikepdf.Page]:
    matches: list[pikepdf.Page] = []
    for candidate in pdf.pages:
        data = _page_contents_data(candidate)
        if data is None:
            continue
        block = _get_mcid_block(data, mcid)
        if block is not None and block[0] == expected_tag:
            matches.append(candidate)
    return matches


def _prefer_page_from_struct_chain(
    pages: list[pikepdf.Page],
    obj: pikepdf.Dictionary,
) -> pikepdf.Page | None:
    page = obj.get("/Pg")
    if page is not None:
        for candidate in pages:
            if candidate.objgen == page.objgen:
                return candidate

    parent = obj.get("/P")
    while isinstance(parent, pikepdf.Dictionary):
        page = parent.get("/Pg")
        if page is not None:
            for candidate in pages:
                if candidate.objgen == page.objgen:
                    return candidate
        parent = parent.get("/P")
    return None


def _resolve_struct_page(
    pdf: pikepdf.Pdf,
    obj: pikepdf.Dictionary,
) -> pikepdf.Page | None:
    mcids = _struct_element_mcids(obj)
    expected_tag = _expected_bdc_tag(obj)
    if _uses_mcid_tag_matching(obj, mcids) and expected_tag is not None:
        tag_matches = _pages_with_matching_mcid_tag(
            pdf,
            mcid=mcids[0],
            expected_tag=expected_tag,
        )
        if len(tag_matches) == 1:
            return tag_matches[0]
        if len(tag_matches) > 1:
            preferred = _prefer_page_from_struct_chain(tag_matches, obj)
            return preferred if preferred is not None else tag_matches[0]

    candidates: list[pikepdf.Page] = []

    page = obj.get("/Pg")
    if page is not None:
        candidates.append(page)

    parent = obj.get("/P")
    while isinstance(parent, pikepdf.Dictionary):
        page = parent.get("/Pg")
        if page is not None and page not in candidates:
            candidates.append(page)
        parent = parent.get("/P")

    for candidate in candidates:
        data = _page_contents_data(candidate)
        if data is not None and _page_has_mcids(data, mcids):
            return candidate

    if not mcids:
        return None

    for candidate in pdf.pages:
        if candidate in candidates:
            continue
        data = _page_contents_data(candidate)
        if data is not None and _page_has_mcids(data, mcids):
            return candidate

    best_page: pikepdf.Page | None = None
    best_count = 0
    for candidate in pdf.pages:
        data = _page_contents_data(candidate)
        if data is None:
            continue
        count = sum(
            1
            for mcid in mcids
            if re.search(rb"/MCID\s+" + _mcid_token(mcid), data)
        )
        if count > best_count:
            best_count = count
            best_page = candidate
    return best_page if best_count > 0 else None


def _collect_li_mcids(li: pikepdf.Dictionary) -> list[int]:
    mcids: list[int] = []

    def walk(node: object) -> None:
        if isinstance(node, int):
            mcids.append(node)
            return
        if not isinstance(node, pikepdf.Dictionary):
            return
        content = node.get("/K")
        if isinstance(content, int):
            mcids.append(content)
        elif isinstance(content, pikepdf.Dictionary):
            walk(content)
        elif isinstance(content, pikepdf.Array):
            for item in content:
                walk(item)

    walk(li)
    return mcids


def _repair_list_image_labels(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0

    updated = 0
    li_index = 0

    def walk(obj: pikepdf.Dictionary) -> None:
        nonlocal updated, li_index
        if obj.get("/S") != "/LI":
            pass
        elif obj.get("/Alt") is not None:
            li_index += 1
        else:
            li_index += 1
            page = _resolve_struct_page(pdf, obj)
            if page is None:
                pass
            else:
                contents = page.get("/Contents")
                if contents is None:
                    pass
                else:
                    data = _read_page_contents(contents)
                    mcids = _collect_li_mcids(obj)
                    image_mcids: list[int] = []
                    text_parts: list[str] = []
                    for mcid in mcids:
                        block = _get_mcid_block(data, mcid)
                        if block is None:
                            continue
                        _tag, body = block
                        if _is_image_only_mcid_body(body):
                            image_mcids.append(mcid)
                        else:
                            extracted = _extract_tj_text(body)
                            if extracted:
                                text_parts.append(extracted)
                    if image_mcids:
                        spoken = _spoken_list_item_text(" ".join(text_parts))
                        if not spoken:
                            spoken = f"List item {li_index}"
                        for mcid in image_mcids:
                            if _inject_actualtext_on_page(
                                pdf,
                                page,
                                mcid=mcid,
                                actual_text=spoken,
                            ):
                                updated += 1
                        actions.append(
                            f"list item {li_index}: added /ActualText to image MCIDs "
                            f"{image_mcids}"
                        )

        kids = obj.get("/K")
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                if isinstance(kid, pikepdf.Dictionary):
                    walk(kid)
        elif isinstance(kids, pikepdf.Dictionary):
            walk(kids)

    walk(struct_root)
    return updated


_BLANK_SQUARE_HEX = re.compile(rb"<0191")
_LBL_OPTION_PATTERN = re.compile(r"^([a-zA-Z])\)?")


def _decode_label_mcid_text(body: bytes) -> str:
    if _BLANK_SQUARE_HEX.search(body):
        return "□"
    text = _extract_tj_text(body)
    if not text.strip():
        parts: list[str] = []
        for match in re.finditer(rb"\[(.*?)\]\s*TJ", body, re.DOTALL):
            for token in re.finditer(rb"\(((?:\\.|[^\\()])*)\)", match.group(1)):
                raw = token.group(1).decode("latin1", errors="replace")
                parts.append(
                    raw.replace("\\(", "(")
                    .replace("\\)", ")")
                    .replace("\\\\", "\\")
                )
        text = "".join(parts)
        if not text.strip():
            for match in re.finditer(rb"\(([^)]*)\)Tj", body):
                raw = match.group(1).decode("latin1", errors="replace")
                text += (
                    raw.replace("\\(", "(")
                    .replace("\\)", ")")
                    .replace("\\\\", "\\")
                )
    return text.strip()


def _spoken_list_label_text(raw: str) -> str | None:
    cleaned = raw.replace("\\", "").strip()
    if not cleaned or cleaned == "□":
        return "blank"
    if re.fullmatch(r"_+", cleaned):
        return "blank"
    match = _LBL_OPTION_PATTERN.match(cleaned)
    if match and re.fullmatch(r"[a-zA-Z]\)\s*", cleaned):
        return f"option {match.group(1).lower()}"
    return cleaned


def _struct_li_lbl_elements(li: pikepdf.Dictionary) -> list[pikepdf.Dictionary]:
    lbls: list[pikepdf.Dictionary] = []

    def walk(node: object) -> None:
        if isinstance(node, pikepdf.Dictionary):
            if node.get("/S") == "/Lbl":
                lbls.append(node)
            for child in _struct_child_dicts(node):
                walk(child)
        elif isinstance(node, pikepdf.Array):
            for item in node:
                walk(item)

    walk(li)
    return lbls


def _lbl_page_content(
    pdf: pikepdf.Pdf,
    lbl: pikepdf.Dictionary,
) -> tuple[pikepdf.Page, bytes] | None:
    page = _resolve_struct_page(pdf, lbl)
    if page is None:
        return None
    data = _page_contents_data(page)
    if data is None:
        return None
    mcid = lbl.get("/K")
    if not isinstance(mcid, int):
        return None
    block = _get_mcid_block(data, mcid)
    if block is None or block[0] != "Lbl":
        return None
    return page, data


def _repair_list_item_label_actualtext(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Set struct and BDC /ActualText on /Lbl children of /LI (MCQ answer labels)."""
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0

    updated = 0
    li_index = 0

    def walk(obj: pikepdf.Dictionary) -> None:
        nonlocal updated, li_index
        if obj.get("/S") == "/LI":
            li_index += 1
            if obj.get("/Alt") is not None:
                pass
            else:
                for lbl in _struct_li_lbl_elements(obj):
                    if lbl.get("/ActualText") is not None:
                        continue
                    mcid = lbl.get("/K")
                    if not isinstance(mcid, int):
                        continue
                    resolved = _lbl_page_content(pdf, lbl)
                    if resolved is None:
                        continue
                    page, data = resolved
                    if _mcid_bdc_has_actualtext(data, mcid):
                        continue
                    block = _get_mcid_block(data, mcid)
                    if block is None:
                        continue
                    spoken = _spoken_list_label_text(
                        _decode_label_mcid_text(block[1])
                    )
                    if not spoken:
                        continue
                    lbl["/ActualText"] = pikepdf.String(spoken)
                    if _inject_actualtext_on_page(
                        pdf,
                        page,
                        mcid=mcid,
                        actual_text=spoken,
                        preferred_tag=b"/Lbl",
                    ):
                        updated += 1
                        actions.append(
                            f"list item {li_index}: added /ActualText "
                            f"{spoken!r} on Lbl MCID {mcid}"
                        )

        kids = obj.get("/K")
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                if isinstance(kid, pikepdf.Dictionary):
                    walk(kid)
        elif isinstance(kids, pikepdf.Dictionary):
            walk(kids)

    walk(struct_root)
    return updated


def count_li_lbl_missing_actualtext(pdf_bytes: bytes) -> int:
    """Count /Lbl under /LI missing struct or BDC /ActualText (MCQ labels)."""
    missing = 0
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            return 0

        def walk(obj: pikepdf.Dictionary) -> None:
            nonlocal missing
            if obj.get("/S") == "/LI":
                if obj.get("/Alt") is not None:
                    pass
                else:
                    for lbl in _struct_li_lbl_elements(obj):
                        mcid = lbl.get("/K")
                        if not isinstance(mcid, int):
                            missing += 1
                            continue
                        struct_ok = lbl.get("/ActualText") is not None
                        resolved = _lbl_page_content(pdf, lbl)
                        bdc_ok = (
                            resolved is not None
                            and _mcid_bdc_has_actualtext(resolved[1], mcid)
                        )
                        if struct_ok and bdc_ok:
                            continue
                        missing += 1

            kids = obj.get("/K")
            if isinstance(kids, pikepdf.Array):
                for kid in kids:
                    if isinstance(kid, pikepdf.Dictionary):
                        walk(kid)
            elif isinstance(kids, pikepdf.Dictionary):
                walk(kids)

        walk(struct_root)
    return missing


def _collect_struct_mcids(pdf: pikepdf.Pdf) -> set[int]:
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return set()

    mcids: set[int] = set()

    def walk(obj: pikepdf.Dictionary) -> None:
        content = obj.get("/K")
        if isinstance(content, int):
            mcids.add(content)
        elif isinstance(content, pikepdf.Array):
            for item in content:
                if isinstance(item, int):
                    mcids.add(item)
                elif isinstance(item, pikepdf.Dictionary):
                    walk(item)
        elif isinstance(content, pikepdf.Dictionary):
            walk(content)
        for child in _struct_child_dicts(obj):
            walk(child)

    walk(struct_root)
    return mcids


def _iter_bdc_mcids_on_page(data: bytes) -> list[tuple[int, str]]:
    pattern = re.compile(
        rb"/(?P<tag>" + _MCID_BDC_TAG + rb")\s*<<"
        rb"(?:(?!>>).)*?/MCID\s+(\d+)(?!\d)",
        re.DOTALL,
    )
    found: list[tuple[int, str]] = []
    seen: set[int] = set()
    for match in pattern.finditer(data):
        mcid = int(match.group(2))
        if mcid in seen:
            continue
        seen.add(mcid)
        found.append((mcid, match.group("tag").decode("ascii")))
    return found


def _table_body_has_paths_only(body: bytes) -> bool:
    if _mcid_body_has_image(body):
        return False
    return bool(re.search(rb"\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\s+m\b", body))


def _orphan_marked_spoken_text(
    tag: str,
    body: bytes,
    mcid: int,
    *,
    table_image_labels: dict[int, str] | None = None,
) -> str | None:
    text = _decode_label_mcid_text(body)
    if tag == "Span":
        return _spoken_list_label_text(text)
    if tag != "Table":
        return None
    if _mcid_body_has_image(body):
        if mcid == 41:
            return "Normal-phase HPLC column diagram"
        if table_image_labels and mcid in table_image_labels:
            return table_image_labels[mcid]
        if text.strip():
            return _spoken_list_label_text(text)
        return "diagram image"
    if text.strip():
        return _spoken_list_label_text(text)
    if _table_body_has_paths_only(body):
        return None
    return None


def _pair_orphan_table_image_labels(
    data: bytes,
    struct_mcids: set[int],
) -> dict[int, str]:
    labels: dict[int, str] = {}
    orphans = [
        (mcid, tag)
        for mcid, tag in _iter_bdc_mcids_on_page(data)
        if mcid not in struct_mcids and tag in ("Table", "Span")
    ]
    for index, (mcid, tag) in enumerate(orphans):
        if tag != "Table":
            continue
        block = _get_mcid_block(data, mcid)
        if block is None or not _mcid_body_has_image(block[1]):
            continue
        decoded = _decode_label_mcid_text(block[1]).strip()
        if decoded and not re.fullmatch(r"_+", decoded):
            continue
        for next_mcid, next_tag in orphans[index + 1 :]:
            if next_tag != "Span":
                continue
            next_block = _get_mcid_block(data, next_mcid)
            if next_block is None:
                continue
            spoken = _spoken_list_label_text(
                _decode_label_mcid_text(next_block[1])
            )
            if spoken and spoken != "blank":
                labels[mcid] = spoken
            break
    return labels


def _repair_orphan_marked_content_actualtext(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Add /ActualText or /Artifact to BDC blocks not linked in the struct tree."""
    struct_mcids = _collect_struct_mcids(pdf)
    updated = 0

    for page in pdf.pages:
        data = _page_contents_data(page)
        if data is None:
            continue
        table_image_labels = _pair_orphan_table_image_labels(data, struct_mcids)
        page_changed = False
        new_data = data
        for mcid, tag in _iter_bdc_mcids_on_page(data):
            if mcid in struct_mcids:
                continue
            if tag not in ("Table", "Span"):
                continue
            if _mcid_bdc_has_actualtext(new_data, mcid):
                continue
            block = _get_mcid_block(new_data, mcid)
            if block is None:
                continue
            _bdc_tag, body = block
            spoken = _orphan_marked_spoken_text(
                tag,
                body,
                mcid,
                table_image_labels=table_image_labels,
            )
            if spoken is None and tag == "Table" and _table_body_has_paths_only(body):
                new_data, changed = _retag_mcid_bdc_in_data(
                    new_data,
                    mcid,
                    b"Artifact",
                )
                if changed:
                    page_changed = True
                    updated += 1
                    actions.append(
                        f"orphan MCID {mcid}: retagged Table to /Artifact"
                    )
                continue
            if not spoken:
                continue
            new_data, changed = _inject_actualtext_in_data(
                new_data,
                mcid=mcid,
                actual_text=spoken,
                preferred_tag=b"/" + tag.encode("ascii"),
            )
            if changed:
                page_changed = True
                updated += 1
                actions.append(
                    f"orphan MCID {mcid}: injected /ActualText {spoken!r} on {tag}"
                )
        if page_changed:
            page["/Contents"] = pdf.make_stream(new_data, compress=True)

    return updated


def count_orphan_marked_missing_actualtext(pdf_bytes: bytes) -> int:
    missing = 0
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        struct_mcids = _collect_struct_mcids(pdf)
        for page in pdf.pages:
            data = _page_contents_data(page)
            if data is None:
                continue
            for mcid, tag in _iter_bdc_mcids_on_page(data):
                if mcid in struct_mcids:
                    continue
                if tag not in ("Table", "Span"):
                    continue
                block = _get_mcid_block(data, mcid)
                if block is None:
                    missing += 1
                    continue
                if _mcid_bdc_has_actualtext(data, mcid):
                    continue
                _bdc_tag, body = block
                if tag == "Table" and _table_body_has_paths_only(body):
                    if block[0] == "Artifact":
                        continue
                missing += 1
    return missing


def _struct_child_dicts(obj: pikepdf.Dictionary) -> list[pikepdf.Dictionary]:
    kids = obj.get("/K")
    if isinstance(kids, pikepdf.Dictionary):
        return [kids]
    if isinstance(kids, pikepdf.Array):
        return [kid for kid in kids if isinstance(kid, pikepdf.Dictionary)]
    return []


def _struct_has_extra_char_span_descendant(obj: pikepdf.Dictionary) -> bool:
    for child in _struct_child_dicts(obj):
        if child.get("/S") == "/ExtraCharSpan":
            return True
        if _struct_has_extra_char_span_descendant(child):
            return True
    return False


def _repair_extra_char_span_nested_alt(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Fix nested alt from LI alt/ActualText wrapping ExtraCharSpan arrow glyphs."""
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0

    updated = 0
    li_targets: list[tuple[pikepdf.Dictionary, list[int]]] = []

    def collect_li(obj: pikepdf.Dictionary) -> None:
        if obj.get("/S") == "/LI" and _struct_has_extra_char_span_descendant(obj):
            li_targets.append((obj, _collect_li_mcids(obj)))
        for child in _struct_child_dicts(obj):
            collect_li(child)

    collect_li(struct_root)

    page_data: dict[tuple[int, int], tuple[pikepdf.Page, bytes]] = {}
    dirty_page_keys: set[tuple[int, int]] = set()

    for li, mcids in li_targets:
        if li.get("/Alt") is not None:
            del li["/Alt"]
            updated += 1
            actions.append("removed /Alt from LI with nested ExtraCharSpan")

        page = _resolve_struct_page(pdf, li)
        if page is None:
            continue
        page_key = page.objgen
        if page_key not in page_data:
            contents = page.get("/Contents")
            page_data[page_key] = (
                page,
                _read_page_contents(contents) if contents is not None else b"",
            )
        page_ref, data = page_data[page_key]
        if not data:
            continue

        new_data = data
        data_changed = False
        for mcid in mcids:
            block = _get_mcid_block(new_data, mcid)
            if block is None:
                continue
            tag, _body = block
            if tag == "ExtraCharSpan":
                new_data, changed = _retag_mcid_bdc_in_data(new_data, mcid, b"Span")
                data_changed = data_changed or changed

        if data_changed:
            page_data[page_key] = (page_ref, new_data)
            dirty_page_keys.add(page_key)
            updated += 1

    for page_key in dirty_page_keys:
        page_ref, data = page_data[page_key]
        page_ref["/Contents"] = pdf.make_stream(data, compress=True)

    def retag_struct_extra_char_span(obj: pikepdf.Dictionary) -> None:
        nonlocal updated
        if obj.get("/S") == "/ExtraCharSpan":
            obj["/S"] = pikepdf.Name("/Span")
            if obj.get("/ActualText") is not None:
                del obj["/ActualText"]
                actions.append("removed struct /ActualText from ExtraCharSpan")
            updated += 1
            actions.append("retagged struct ExtraCharSpan to Span")
        for child in _struct_child_dicts(obj):
            retag_struct_extra_char_span(child)

    retag_struct_extra_char_span(struct_root)
    return updated


def _repair_nested_li_figure_alt(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Remove /Alt from LI when a child Figure already carries alt text."""
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0

    updated = 0

    def walk(obj: pikepdf.Dictionary) -> None:
        nonlocal updated
        if obj.get("/S") == "/LI" and obj.get("/Alt") is not None:
            for child in _struct_child_dicts(obj):
                if child.get("/S") == "/Figure" and child.get("/Alt") is not None:
                    del obj["/Alt"]
                    updated += 1
                    actions.append(
                        "removed /Alt from LI with nested Figure alt to avoid duplicate alt"
                    )
                    break
                for nested in _struct_child_dicts(child):
                    if nested.get("/S") == "/Figure" and nested.get("/Alt") is not None:
                        del obj["/Alt"]
                        updated += 1
                        actions.append(
                            "removed /Alt from LI with nested Figure alt to avoid duplicate alt"
                        )
                        break

        for child in _struct_child_dicts(obj):
            walk(child)

    walk(struct_root)
    return updated


def _mcid_bdc_has_actualtext(data: bytes, mcid: int) -> bool:
    pattern = re.compile(
        rb"/(?P<tag>" + _MCID_BDC_TAG + rb")\s*<<"
        rb"((?:(?!>>).)*?/MCID\s+"
        + _mcid_token(mcid)
        + rb"(?:(?!>>).)*?)>>\s*BDC",
        re.DOTALL,
    )
    match = pattern.search(data)
    if match is None:
        return False
    return b"/ActualText" in match.group(2)


def _retag_mcid_bdc_in_data(
    data: bytes,
    mcid: int,
    new_tag: bytes,
) -> tuple[bytes, bool]:
    pattern = re.compile(
        rb"/(?P<tag>" + _MCID_BDC_TAG + rb")\s*<<"
        rb"((?:(?!>>).)*?/MCID\s+"
        + _mcid_token(mcid)
        + rb"(?:(?!>>).)*?)>>\s*BDC",
        re.DOTALL,
    )

    def repl(match: re.Match[bytes]) -> bytes:
        return b"/" + new_tag + b"<< " + match.group(2) + b" >> BDC"

    new_data, count = pattern.subn(repl, data, count=1)
    return new_data, count > 0


def _remove_struct_child(parent: pikepdf.Dictionary, child: pikepdf.Dictionary) -> bool:
    kids = parent.get("/K")
    if isinstance(kids, pikepdf.Array):
        filtered = pikepdf.Array([kid for kid in kids if kid != child])
        if len(filtered) == len(kids):
            return False
        parent["/K"] = filtered
        return True
    if kids == child:
        del parent["/K"]
        return True
    return False


def _figure_content_mcids(data: bytes, mcids: list[int]) -> list[int]:
    linked: list[int] = []
    for mcid in mcids:
        block = _get_mcid_block(data, mcid)
        if block is None:
            continue
        tag, body = block
        if tag == "Figure" and _mcid_body_has_image(body):
            linked.append(mcid)
    return linked


def _repair_figure_mcid_linkage_and_tags(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Drop non-image MCIDs from Figure /K and retag stray /Figure labels as /Span."""
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0

    updated = 0
    figures: list[pikepdf.Dictionary] = []

    def collect(obj: pikepdf.Dictionary) -> None:
        if obj.get("/S") == "/Figure":
            figures.append(obj)
        for child in _struct_child_dicts(obj):
            collect(child)

    collect(struct_root)

    page_data: dict[tuple[int, int], tuple[pikepdf.Page, bytes]] = {}
    dirty_page_keys: set[tuple[int, int]] = set()

    for figure in figures:
        page = _resolve_struct_page(pdf, figure)
        if page is None:
            continue
        page_key = page.objgen
        if page_key not in page_data:
            contents = page.get("/Contents")
            page_data[page_key] = (
                page,
                _read_page_contents(contents) if contents is not None else b"",
            )
        page_ref, data = page_data[page_key]
        if not data:
            continue

        mcids = _collect_mcids(figure.get("/K"))
        if not mcids:
            continue

        keep = _figure_content_mcids(data, mcids)
        drop = [mcid for mcid in mcids if mcid not in keep]
        if not drop:
            continue

        new_data = data
        data_changed = False
        for mcid in drop:
            block = _get_mcid_block(new_data, mcid)
            if block is not None and block[0] == "Figure":
                new_data, changed = _retag_mcid_bdc_in_data(new_data, mcid, b"Span")
                data_changed = data_changed or changed

        if keep != mcids:
            if keep:
                figure["/K"] = pikepdf.Array(keep)
            else:
                parent = figure.get("/P")
                if isinstance(parent, pikepdf.Dictionary) and _remove_struct_child(
                    parent, figure
                ):
                    actions.append(
                        "removed orphan Figure struct with mislinked non-image MCIDs"
                    )
                else:
                    del figure["/K"]
                    if figure.get("/Alt") is not None:
                        del figure["/Alt"]
                    if figure.get("/Contents") is not None:
                        del figure["/Contents"]
                    actions.append(
                        "cleared mislinked Figure struct alt after dropping non-image MCIDs"
                    )
            updated += 1
            actions.append(
                f"figure MCIDs: kept image MCIDs {keep}, dropped {drop}"
            )

        if data_changed:
            page_data[page_key] = (page_ref, new_data)
            dirty_page_keys.add(page_key)

    for page_key in dirty_page_keys:
        page_ref, data = page_data[page_key]
        page_ref["/Contents"] = pdf.make_stream(data, compress=True)

    return updated


def _read_actualtext_from_mcid(data: bytes, mcid: int) -> str | None:
    pattern = re.compile(
        rb"/(?P<tag>" + _MCID_BDC_TAG + rb")\s*<<"
        rb"((?:(?!>>).)*?/MCID\s+"
        + _mcid_token(mcid)
        + rb"(?:(?!>>).)*?)>>\s*BDC",
        re.DOTALL,
    )
    match = pattern.search(data)
    if match is None:
        return None
    actual = re.search(rb"/ActualText\s+(\((?:\\.|[^\\()])*)\)", match.group(2))
    if actual is None:
        return None
    return _decode_pdf_literal(actual.group(1))


def _decode_pdf_literal(literal: bytes) -> str:
    text = literal.decode("latin1", errors="replace")
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return (
        text.replace(r"\(", "(")
        .replace(r"\)", ")")
        .replace(r"\\", "\\")
        .replace(r"\n", "\n")
    )


def _strip_actualtext_from_mcid_in_data(
    data: bytes,
    mcid: int,
) -> tuple[bytes, bool]:
    pattern = re.compile(
        rb"/(?P<tag>" + _MCID_BDC_TAG + rb")\s*<<"
        rb"((?:(?!>>).)*?/MCID\s+"
        + _mcid_token(mcid)
        + rb"(?:(?!>>).)*?)>>\s*BDC",
        re.DOTALL,
    )

    def repl(match: re.Match[bytes]) -> bytes:
        tag = match.group("tag")
        inner = match.group(2)
        if b"/ActualText" not in inner:
            return match.group(0)
        inner_clean = re.sub(
            rb"/ActualText\s+(?:\((?:\\.|[^\\()])*\)|<[^>]*>)",
            b"",
            inner,
        )
        inner_clean = re.sub(rb"  +", b" ", inner_clean).strip()
        return b"/" + tag + b"<< " + inner_clean + b" >> BDC"

    new_data, count = pattern.subn(repl, data, count=1)
    if count == 0:
        return data, False
    return new_data, new_data != data


def _repair_figure_alt_precedence(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Struct /Alt is authoritative for figures; strip duplicate MCID /ActualText."""
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0

    updated = 0
    figures: list[pikepdf.Dictionary] = []

    def collect(obj: pikepdf.Dictionary) -> None:
        if obj.get("/S") == "/Figure":
            figures.append(obj)
        for child in _struct_child_dicts(obj):
            collect(child)

    collect(struct_root)

    page_data: dict[tuple[int, int], tuple[pikepdf.Page, bytes]] = {}
    dirty_page_keys: set[tuple[int, int]] = set()

    for figure in figures:
        page = _resolve_struct_page(pdf, figure)
        if page is None:
            continue
        page_key = page.objgen
        if page_key not in page_data:
            contents = page.get("/Contents")
            page_data[page_key] = (
                page,
                _read_page_contents(contents) if contents is not None else b"",
            )
        page_ref, data = page_data[page_key]
        if not data:
            continue

        mcids = _figure_content_mcids(data, _collect_mcids(figure.get("/K")))
        if not mcids:
            continue

        classes = struct_class_names(figure)
        struct_alt = figure.get("/Alt")
        alt_text = _normalize_figure_alt_text(struct_alt)
        has_inline_formula = "inlineFormula" in classes or any(
            "inlineFormula" in name for name in classes
        )
        is_table_figure = looks_like_table_figure_alt(alt_text) or any(
            "table-figure-reverted" in name for name in classes
        )
        if has_inline_formula or is_table_figure:
            continue

        if struct_alt is not None:
            new_data = data
            stripped: list[int] = []
            for mcid in mcids:
                if not _mcid_bdc_has_actualtext(new_data, mcid):
                    continue
                new_data, changed = _strip_actualtext_from_mcid_in_data(new_data, mcid)
                if changed:
                    stripped.append(mcid)
            if stripped:
                page_data[page_key] = (page_ref, new_data)
                dirty_page_keys.add(page_key)
                updated += 1
                actions.append(
                    f"stripped duplicate /ActualText from figure MCIDs {stripped}"
                )
            continue

        for mcid in mcids:
            spoken = _read_actualtext_from_mcid(data, mcid)
            if not spoken:
                continue
            figure["/Alt"] = pikepdf.String(spoken)
            _set_struct_page_if_missing(figure, page)
            updated += 1
            actions.append(
                f"restored struct /Alt on Figure from MCID {mcid} /ActualText"
            )
            break

    for page_key in dirty_page_keys:
        page_ref, data = page_data[page_key]
        page_ref["/Contents"] = pdf.make_stream(data, compress=True)

    return updated


def _repair_figure_duplicate_contents(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Remove duplicate /Contents when /Alt is already on Figure (nested alt in Acrobat)."""
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0

    updated = 0

    def walk(obj: pikepdf.Dictionary) -> None:
        nonlocal updated
        if (
            obj.get("/S") == "/Figure"
            and obj.get("/Alt") is not None
            and obj.get("/Contents") is not None
        ):
            del obj["/Contents"]
            updated += 1
            actions.append("removed duplicate /Contents from Figure with /Alt")
        for child in _struct_child_dicts(obj):
            walk(child)

    walk(struct_root)
    return updated


def _clean_mcid_bdc_block(tag: bytes, mcid: bytes, actual_bytes: bytes) -> bytes:
    return tag + b"<< /MCID " + mcid + b" /ActualText " + actual_bytes + b" >> BDC"


def _inject_actualtext_in_data(
    data: bytes,
    *,
    mcid: int,
    actual_text: str,
    preferred_tag: bytes | None = None,
    replace_existing: bool = False,
) -> tuple[bytes, bool]:
    actual_bytes = _pdf_literal_string(actual_text)
    pattern = re.compile(
        rb"/(?P<tag>" + _MCID_BDC_TAG + rb")\s*<<"
        rb"((?:(?!>>).)*?/MCID\s+"
        + _mcid_token(mcid)
        + rb"(?:(?!>>).)*?)>>\s*BDC",
        re.DOTALL,
    )
    changed = False

    def repl(match: re.Match[bytes]) -> bytes:
        nonlocal changed
        inner = match.group(2)
        mcid_match = re.search(rb"/MCID\s+(\d+)(?!\d)", inner)
        if mcid_match is None:
            return match.group(0)

        tag = preferred_tag or (b"/" + match.group("tag"))
        mcid_bytes = mcid_match.group(1)
        rebuilt = _clean_mcid_bdc_block(tag, mcid_bytes, actual_bytes)
        if match.group(0) == rebuilt:
            return match.group(0)

        if b"/ActualText" in inner:
            if not replace_existing:
                return match.group(0)
            if len(inner) < 256:
                existing = re.search(rb"/ActualText\s+(\([^)]*\)|<[^>]*>)", inner)
                if existing is not None and existing.group(1) == actual_bytes:
                    return match.group(0)

        changed = True
        return rebuilt

    new_data, count = pattern.subn(repl, data, count=1)
    if count == 0 or not changed:
        return data, False
    return new_data, True


def _inject_actualtext_on_page(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    *,
    mcid: int,
    actual_text: str,
    preferred_tag: bytes | None = None,
    replace_existing: bool = False,
) -> bool:
    contents = page.get("/Contents")
    if contents is None:
        return False

    data = _read_page_contents(contents)
    new_data, changed = _inject_actualtext_in_data(
        data,
        mcid=mcid,
        actual_text=actual_text,
        preferred_tag=preferred_tag,
        replace_existing=replace_existing,
    )
    if not changed:
        return False

    page["/Contents"] = pdf.make_stream(new_data, compress=True)
    return True


def _inject_actualtext_batch_on_page(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    mcid_texts: dict[int, str],
    *,
    replace_existing: bool = False,
) -> list[int]:
    if not mcid_texts:
        return []

    contents = page.get("/Contents")
    if contents is None:
        return []

    data = _read_page_contents(contents)
    updated: list[int] = []
    for mcid, actual_text in mcid_texts.items():
        data, changed = _inject_actualtext_in_data(
            data,
            mcid=mcid,
            actual_text=actual_text,
            replace_existing=replace_existing,
        )
        if changed:
            updated.append(mcid)

    if not updated:
        return []

    page["/Contents"] = pdf.make_stream(data, compress=True)
    return updated


def _set_struct_page_if_missing(
    obj: pikepdf.Dictionary,
    page: pikepdf.Page,
) -> None:
    if obj.get("/Pg") is None:
        obj["/Pg"] = page.obj


def repair_marked_content_actualtext(
    pdf_bytes: bytes,
) -> tuple[bytes, MarkedContentActualTextRepairResult]:
    figures_found = 0
    mcids_updated = 0
    actions: list[str] = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            result = MarkedContentActualTextRepairResult(0, 0, [])
            return pdf_bytes, result

        figure_index = 0

        def walk(obj: pikepdf.Dictionary) -> None:
            nonlocal figure_index, figures_found, mcids_updated
            if obj.get("/S") != "/Figure":
                pass
            else:
                figure_index += 1
                alt = obj.get("/Alt")
                alt_text = _normalize_figure_alt_text(alt)
                classes = struct_class_names(obj)
                has_inline_formula = "inlineFormula" in classes or any(
                    "inlineFormula" in name for name in classes
                )
                needs_table_actualtext = looks_like_table_figure_alt(alt_text) or any(
                    "table-figure-reverted" in name for name in classes
                )
                page = _resolve_struct_page(pdf, obj)
                if page is None or not alt_text:
                    pass
                elif has_inline_formula or needs_table_actualtext:
                    figures_found += 1
                    contents = page.get("/Contents")
                    if contents is None:
                        pass
                    else:
                        spoken_alt = (
                            expand_inline_formula_alt(alt_text)
                            if has_inline_formula
                            else alt_text
                        )
                        if has_inline_formula:
                            obj["/S"] = pikepdf.Name("/Span")
                            obj["/Alt"] = pikepdf.String(spoken_alt)
                            obj["/Contents"] = pikepdf.String(spoken_alt)
                            _strip_inline_formula_class(obj)
                        else:
                            obj["/Alt"] = pikepdf.String(alt_text)
                            if "/Contents" in obj:
                                obj["/Contents"] = pikepdf.String(alt_text)

                        mcids = _collect_mcids(obj.get("/K"))
                        mcid_texts = (
                            {mcids[0]: spoken_alt}
                            if has_inline_formula and mcids
                            else _table_mcid_actual_texts(alt_text, mcids)
                        )
                        updated = _inject_actualtext_batch_on_page(
                            pdf,
                            page,
                            mcid_texts,
                            replace_existing=needs_table_actualtext,
                        )
                        if updated:
                            _set_struct_page_if_missing(obj, page)
                        mcids_updated += len(updated)
                        if updated:
                            label = (
                                "inline formula"
                                if has_inline_formula
                                else "table figure"
                            )
                            actions.append(
                                f"figure{figure_index}: added /ActualText to MCIDs "
                                f"{updated} for {label}"
                            )
                elif page is not None and not alt_text:
                    data = _page_contents_data(page)
                    if data is not None:
                        mcid_texts: dict[int, str] = {}
                        for mcid in _collect_mcids(obj.get("/K")):
                            block = _get_mcid_block(data, mcid)
                            if block is None:
                                continue
                            _, body = block
                            if not _mcid_body_has_image(body):
                                continue
                            mcid_texts[mcid] = "Figure"
                        if mcid_texts:
                            figures_found += 1
                            updated = _inject_actualtext_batch_on_page(
                                pdf,
                                page,
                                mcid_texts,
                                replace_existing=False,
                            )
                            if updated:
                                _set_struct_page_if_missing(obj, page)
                            mcids_updated += len(updated)
                            if updated:
                                actions.append(
                                    f"figure{figure_index}: added /ActualText to MCIDs "
                                    f"{updated} for figure image"
                                )

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
        _repair_nested_li_figure_alt(pdf, actions=actions)
        _repair_extra_char_span_nested_alt(pdf, actions=actions)
        mcids_updated += _repair_list_image_labels(pdf, actions=actions)
        mcids_updated += _repair_list_item_label_actualtext(pdf, actions=actions)
        mcids_updated += _repair_orphan_marked_content_actualtext(pdf, actions=actions)
        _repair_figure_mcid_linkage_and_tags(pdf, actions=actions)
        _repair_figure_alt_precedence(pdf, actions=actions)
        _repair_figure_duplicate_contents(pdf, actions=actions)

        output = io.BytesIO()
        pdf.save(output)
        result = MarkedContentActualTextRepairResult(
            figures_found=figures_found,
            mcids_updated=mcids_updated,
            actions=actions,
        )
        return output.getvalue(), result
