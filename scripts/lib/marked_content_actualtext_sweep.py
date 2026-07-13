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


_MCID_BDC_TAG = rb"(?:Figure|Span|P|TD|TH|Formula|LBody|LI|Lbl|StyleSpan)"


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


def _is_image_only_mcid_body(body: bytes) -> bool:
    text = body.decode("latin1", errors="replace")
    has_image = bool(re.search(r"/Im\d+\s+Do", text))
    has_text = bool(re.search(r"\([^()\\]{2,}\)\s*Tj", text))
    return has_image and not has_text


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


def _resolve_struct_page(
    pdf: pikepdf.Pdf,
    obj: pikepdf.Dictionary,
) -> pikepdf.Page | None:
    mcids = _collect_mcids(obj.get("/K"))
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
                        obj["/Alt"] = pikepdf.String(spoken)
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
        mcids_updated += _repair_list_image_labels(pdf, actions=actions)

        output = io.BytesIO()
        pdf.save(output)
        result = MarkedContentActualTextRepairResult(
            figures_found=figures_found,
            mcids_updated=mcids_updated,
            actions=actions,
        )
        return output.getvalue(), result
