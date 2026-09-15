"""Add /ActualText to marked-content MCIDs for Adobe Other-elements alt checks."""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass

import pikepdf
import pymupdf

from lib.figure_alt_quality import (
    classify_figure_alt,
    looks_like_table_figure_alt,
    struct_class_names,
)
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
    # A PDF literal string's \ddd escape is at most 3 OCTAL digits, i.e. one
    # byte (0-255). Any character above that (all astral chars, and BMP
    # chars above U+00FF) can't survive this form: octal-formatting its full
    # codepoint overflows 3 digits, and a reader (or our own regex parsers)
    # then consumes only the first 3 as the escape and leaves the rest as
    # literal digit characters, e.g. "\352066" for U+1D436 decodes as "ê066"
    # (0xEA) instead of the intended character. Fall back to a UTF-16BE hex
    # string, which every ActualText reader in this codebase also accepts.
    if any(ord(char) > 255 for char in text):
        utf16 = text.encode("utf-16-be")
        return b"<FEFF" + utf16.hex().upper().encode("ascii") + b">"
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


_MCID_BDC_TAG = rb"(?:Figure|Span|P|TD|TH|Formula|LBody|LI|Lbl|StyleSpan|ExtraCharSpan|Table|Artifact|Link)"


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
    return bool(re.search(rb"/[^\s/<>{}\[\]()]+\s+Do\b", body))


_LITERAL_STRING = r"\((?:\\.|[^\\()])*\)"
_HEX_STRING = r"<[0-9A-Fa-f\s]+>"
_SHOW_STRING = rf"(?:{_LITERAL_STRING}|{_HEX_STRING})"
_SIMPLE_SHOW_OP = re.compile(rf"{_SHOW_STRING}\s*(?:Tj|'|\")")
_ARRAY_SHOW_OP = re.compile(rf"\[(?:[^\[\]()<>]|{_SHOW_STRING})*\]\s*TJ")


def _decoded_literal_string(raw: str) -> str:
    def unescape(match: re.Match[str]) -> str:
        escape = match.group(1)
        if escape[0] in "01234567":
            return chr(int(escape, 8) & 0xFF)
        return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(
            escape, escape
        )

    return re.sub(r"\\([0-7]{1,3}|.)", unescape, raw)


def _mcid_body_shows_text(body: bytes) -> bool:
    """True when any text-show operator (Tj, ', \", TJ) paints visible text.

    Literal strings are unescaped and must contain a non-whitespace character;
    hex strings count as visible outright (their glyph codes are opaque here).
    """
    text = body.decode("latin1", errors="replace")
    fragments: list[str] = []
    for pattern in (_SIMPLE_SHOW_OP, _ARRAY_SHOW_OP):
        fragments.extend(match.group(0) for match in pattern.finditer(text))
    for fragment in fragments:
        if re.search(_HEX_STRING, fragment):
            return True
        for literal in re.finditer(_LITERAL_STRING, fragment):
            if _decoded_literal_string(literal.group(0)[1:-1]).strip():
                return True
    return False


def _mcid_block_shows_text(data: bytes, mcid: int) -> bool:
    block = _get_mcid_block_to_emc(data, mcid)
    return block is not None and _mcid_body_shows_text(block[2])


def _is_image_only_mcid_body(body: bytes) -> bool:
    return _mcid_body_has_image(body) and not _mcid_body_shows_text(body)


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


def _parent_tree_owner(
    pdf: pikepdf.Pdf,
    page: pikepdf.Dictionary | pikepdf.Page,
    mcid: int,
) -> pikepdf.Dictionary | None:
    """Return the struct element the page's ParentTree entry names for mcid."""
    struct_root = pdf.Root.get("/StructTreeRoot")
    if not isinstance(struct_root, pikepdf.Dictionary):
        return None
    tree = struct_root.get("/ParentTree")
    page_obj = page.obj if isinstance(page, pikepdf.Page) else page
    struct_parents = page_obj.get("/StructParents")
    if not isinstance(tree, pikepdf.Dictionary) or not isinstance(struct_parents, int):
        return None
    key = int(struct_parents)

    def lookup(node: pikepdf.Dictionary, depth: int = 0) -> object:
        if depth > 32:
            return None
        nums = node.get("/Nums")
        if isinstance(nums, pikepdf.Array):
            for index in range(0, len(nums) - 1, 2):
                if isinstance(nums[index], int) and int(nums[index]) == key:
                    return nums[index + 1]
        kids = node.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                if not isinstance(kid, pikepdf.Dictionary):
                    continue
                limits = kid.get("/Limits")
                if (
                    isinstance(limits, pikepdf.Array)
                    and len(limits) == 2
                    and isinstance(limits[0], int)
                    and isinstance(limits[1], int)
                    and not (int(limits[0]) <= key <= int(limits[1]))
                ):
                    continue
                found = lookup(kid, depth + 1)
                if found is not None:
                    return found
        return None

    entry = lookup(tree)
    if not isinstance(entry, pikepdf.Array) or mcid < 0 or mcid >= len(entry):
        return None
    owner = entry[mcid]
    return owner if isinstance(owner, pikepdf.Dictionary) else None


def _resolve_struct_page(
    pdf: pikepdf.Pdf,
    obj: pikepdf.Dictionary,
) -> pikepdf.Page | None:
    mcids = _struct_element_mcids(obj)
    expected_tag = _expected_bdc_tag(obj)
    declared_page = obj.get("/Pg")
    if isinstance(declared_page, pikepdf.Dictionary) and len(mcids) == 1 and obj.is_indirect:
        # The page's ParentTree is the file's own statement of who owns the
        # MCID; when it names this element, a content-stream BDC tag that
        # disagrees with /S (a Lbl block owned by a Span) is not evidence
        # the element lives on another page.
        owner = _parent_tree_owner(pdf, declared_page, mcids[0])
        if owner is not None and owner.is_indirect and owner.objgen == obj.objgen:
            for candidate in pdf.pages:
                if candidate.objgen == declared_page.objgen:
                    return candidate
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
    figure_alt_mcids: set[int] = set()

    def collect_figure_alt_mcids(obj: pikepdf.Dictionary) -> None:
        if obj.get("/S") == "/Figure" and obj.get("/Alt") is not None:
            figure_alt_mcids.update(_collect_mcids(obj.get("/K")))
        kids = obj.get("/K")
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                if isinstance(kid, pikepdf.Dictionary):
                    collect_figure_alt_mcids(kid)
        elif isinstance(kids, pikepdf.Dictionary):
            collect_figure_alt_mcids(kids)

    collect_figure_alt_mcids(struct_root)

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
                    for mcid in mcids:
                        block = _get_mcid_block(data, mcid)
                        if block is None:
                            continue
                        _tag, body = block
                        if _is_image_only_mcid_body(body):
                            if mcid not in figure_alt_mcids:
                                image_mcids.append(mcid)
                    if image_mcids:
                        # The item's text MCIDs are already spoken as text;
                        # echoing them onto every inline image reads the
                        # whole item once per image, so images get the
                        # same short label the untagged-image repair uses.
                        spoken = _UNTAGGED_IMAGE_FALLBACK_ACTUALTEXT
                        updated_mcids: list[int] = []
                        for mcid in image_mcids:
                            if _inject_actualtext_on_page(
                                pdf,
                                page,
                                mcid=mcid,
                                actual_text=spoken,
                            ):
                                updated += 1
                                updated_mcids.append(mcid)
                        if updated_mcids:
                            actions.append(
                                f"list item {li_index}: added /ActualText to image MCIDs "
                                f"{updated_mcids}"
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
_BLANK_SQUARE_HEX_PREFIX = "0191"
_HEX_SHOW_STRING = re.compile(rb"<[0-9A-Fa-f\s]+>")
_LBL_OPTION_PATTERN = re.compile(r"^([a-zA-Z])\)?")


def _decode_label_mcid_text(
    body: bytes,
    *,
    font_code_maps: dict[str, FontCodeMap] | None = None,
) -> str | None:
    """The characters a list label paints, or None when they cannot be read.

    A hex show carries glyph codes, so it is read through the font's
    /ToUnicode rather than guessed from the code. Code 0191 stays the empty
    answer box only where no font names it; trusting the code alone speaks a
    bullet as "blank".

    A label whose codes no font names is unreadable, which is not the same as
    a label that paints nothing: returning "" for it would have
    `_spoken_list_label_text` call it "blank" on no evidence at all.
    """
    if _HEX_SHOW_STRING.search(body):
        decoded = _decode_shown_text_in_order(body, font_code_maps=font_code_maps)
        if decoded is not None:
            return decoded.strip()
        return "□" if _BLANK_SQUARE_HEX.search(body) else None
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


_ANY_SHOW_OP = re.compile(
    rf"(?:\[(?:[^\[\]()<>]|{_SHOW_STRING})*\]\s*TJ)"
    rf"|(?:{_SHOW_STRING}\s*(?:Tj|'|\"))"
)


_CODESPACE_SECTION = re.compile(
    r"begincodespacerange\s*(.*?)endcodespacerange", re.DOTALL
)
_BF_SECTION = re.compile(
    r"begin(?P<kind>bfchar|bfrange)\s*(?P<body>.*?)end(?P=kind)", re.DOTALL
)
_HEX_PAIR = re.compile(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_BFRANGE_ENTRY = re.compile(
    r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:<([0-9A-Fa-f]+)>|\[([^\]]*)\])"
)

# Glyph codes → Unicode text for one font, plus the code byte widths to try.
FontCodeMap = tuple[dict[bytes, str], tuple[int, ...]]


def _is_private_use(char: str) -> bool:
    return 0xE000 <= ord(char) <= 0xF8FF


def _is_encoding_placeholder(char: str) -> bool:
    # The characterEncoding stage renames an unreliable /ToUnicode destination
    # to a Box Drawing character so extraction stops yielding U+FFFD; the
    # glyph is no better named than before.
    return 0x2500 <= ord(char) <= 0x257F


def _tounicode_text(dst: str) -> str | None:
    if len(dst) % 4:
        return None
    text = bytes.fromhex(dst).decode("utf-16-be", errors="replace")
    # A Private Use codepoint is the font declining to name the glyph: Word
    # maps Cambria Math's stretchy delimiters to U+F000-U+F004, which no
    # reader can speak. Treat the code as unmapped so the block stays
    # undecodable rather than guessing a delimiter or dropping it. The same
    # goes for the placeholder the encoding stage swaps in on a later pass,
    # or the second sweep would speak what the first refused.
    if any(
        char == "\ufffd"
        or (ord(char) < 0x20 and not char.isspace())
        or _is_private_use(char)
        or _is_encoding_placeholder(char)
        for char in text
    ):
        return None
    return text


def _font_code_map(font: object) -> FontCodeMap | None:
    """Build a glyph-code → text map from a font's /ToUnicode CMap.

    Returns None for a font without a readable CMap, so hex strings shown
    with it stay undecodable (and the block keeps its glyphs unsilenced).
    """
    if not isinstance(font, pikepdf.Dictionary):
        return None
    cmap = font.get("/ToUnicode")
    if not isinstance(cmap, pikepdf.Stream):
        return None
    try:
        text = cmap.read_bytes().decode("latin1", errors="replace")
    except pikepdf.PdfError:
        return None
    mapping: dict[bytes, str] = {}
    for section in _BF_SECTION.finditer(text):
        body = section.group("body")
        if section.group("kind") == "bfchar":
            for src, dst in _HEX_PAIR.findall(body):
                if len(src) % 2:
                    continue
                spoken = _tounicode_text(dst)
                if spoken is not None:
                    mapping[bytes.fromhex(src)] = spoken
            continue
        for lo, hi, scalar, array_body in _BFRANGE_ENTRY.findall(body):
            if len(lo) % 2 or len(lo) != len(hi):
                continue
            first, last = int(lo, 16), int(hi, 16)
            if last < first or last - first > 0xFFFF:
                continue
            width = len(lo) // 2
            if array_body:
                dsts = re.findall(r"<([0-9A-Fa-f]+)>", array_body)
                for offset, dst in enumerate(dsts[: last - first + 1]):
                    spoken = _tounicode_text(dst)
                    if spoken is not None:
                        mapping[(first + offset).to_bytes(width, "big")] = spoken
                continue
            base = int(scalar, 16)
            for offset in range(last - first + 1):
                spoken = _tounicode_text(format(base + offset, f"0{len(scalar)}X"))
                if spoken is not None:
                    mapping[(first + offset).to_bytes(width, "big")] = spoken
    if not mapping:
        return None
    encoding = font.get("/Encoding")
    if font.get("/Subtype") == "/Type0" and str(encoding).startswith("/Identity"):
        widths: tuple[int, ...] = (2,)
    else:
        widths = tuple(sorted({len(code) for code in mapping}))
    return mapping, widths


def _page_font_code_maps(
    page: pikepdf.Page | pikepdf.Dictionary,
) -> dict[str, FontCodeMap]:
    """Map each /Font resource name on the page to its ToUnicode code map."""
    node: object = page.obj if isinstance(page, pikepdf.Page) else page
    fonts: dict[str, FontCodeMap] = {}
    seen = 0
    while isinstance(node, pikepdf.Dictionary) and seen < 64:
        resources = node.get("/Resources")
        font_dict = (
            resources.get("/Font")
            if isinstance(resources, pikepdf.Dictionary)
            else None
        )
        if isinstance(font_dict, pikepdf.Dictionary):
            for name, font in font_dict.items():
                code_map = _font_code_map(font)
                if code_map is not None:
                    fonts.setdefault(str(name).lstrip("/"), code_map)
            break
        node = node.get("/Parent")
        seen += 1
    return fonts


def _decode_hex_string_with_font(hex_body: str, code_map: FontCodeMap) -> str | None:
    if len(hex_body) % 2:
        hex_body += "0"
    data = bytes.fromhex(hex_body)
    mapping, widths = code_map
    parts: list[str] = []
    index = 0
    while index < len(data):
        for width in widths:
            code = data[index : index + width]
            if len(code) == width and code in mapping:
                parts.append(mapping[code])
                index += width
                break
        else:
            return None
    return "".join(parts)


# Content-stream tokens that change which font a later show op paints with.
_FONT_STATE_TOKEN = re.compile(
    rf"(?P<show>{_ANY_SHOW_OP.pattern})"
    r"|(?P<push>\bq\b)|(?P<pop>\bQ\b)"
    r"|/(?P<font>[^\s/<>\[\]()]+)\s+[-+]?[\d.]+\s+Tf\b"
)


def _font_in_effect_at(data: bytes, offset: int) -> str | None:
    """Return the font resource name selected when the stream reaches offset."""
    text = data[:offset].decode("latin1", errors="replace")
    font: str | None = None
    stack: list[str | None] = []
    for token in _FONT_STATE_TOKEN.finditer(text):
        if token.group("push"):
            stack.append(font)
        elif token.group("pop"):
            if stack:
                font = stack.pop()
        elif token.group("font"):
            font = token.group("font")
    return font


def _decode_shown_text_in_order(
    body: bytes,
    *,
    font_code_maps: dict[str, FontCodeMap] | None = None,
    initial_font: str | None = None,
) -> str | None:
    """Concatenate every text-show string in content-stream order.

    Hex strings are read through the ToUnicode CMap of the font in effect
    (tracked across Tf and q/Q). Returns None when a show op paints glyphs
    this decoder cannot read (a hex-string show with no readable CMap for
    its font, other than the blank-square marker): a caller about to stamp
    block-level /ActualText must not silence glyphs it cannot speak.
    """
    text = body.decode("latin1", errors="replace")
    parts: list[str] = []
    font = initial_font
    stack: list[str | None] = []
    for token in _FONT_STATE_TOKEN.finditer(text):
        if token.group("push"):
            stack.append(font)
            continue
        if token.group("pop"):
            if stack:
                font = stack.pop()
            continue
        if token.group("font"):
            font = token.group("font")
            continue
        for string in re.finditer(
            rf"(?P<lit>{_LITERAL_STRING})|(?P<hex>{_HEX_STRING})", token.group("show")
        ):
            if string.group("lit") is not None:
                parts.append(_decoded_literal_string(string.group("lit")[1:-1]))
                continue
            hex_body = re.sub(r"\s+", "", string.group("hex")[1:-1])
            code_map = (font_code_maps or {}).get(font or "")
            decoded = (
                _decode_hex_string_with_font(hex_body, code_map)
                if code_map is not None
                else None
            )
            if decoded is not None:
                parts.append(decoded)
                continue
            # 0191 is the empty answer box only where the font declines to
            # name the code. A font that does name it outranks the
            # convention: the same code carries a bullet elsewhere, and
            # reading that as the box speaks it "blank".
            if hex_body.upper().startswith(_BLANK_SQUARE_HEX_PREFIX):
                parts.append("□")
                continue
            return None
    return "".join(parts)


def _spoken_list_label_text(
    raw: str | None, *, normalized_only: bool = False
) -> str | None:
    if raw is None:
        # Glyphs the decoder could not read. Nothing is safely speakable.
        return None
    cleaned = raw.replace("\\", "").strip()
    if not cleaned or cleaned == "□":
        return "blank"
    if re.fullmatch(r"_+", cleaned):
        return "blank"
    match = _LBL_OPTION_PATTERN.match(cleaned)
    if match and re.fullmatch(r"[a-zA-Z]\)\s*", cleaned):
        return f"option {match.group(1).lower()}"
    if normalized_only:
        # The raw decode reads content-stream bytes, not ToUnicode: for a
        # symbolic font those bytes are glyph codes, so echoing them as
        # /ActualText stamps garbage over the label's real spoken text —
        # and for a decodable font the echo adds nothing AT can't already
        # read from the glyphs.
        return None
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
                        _decode_label_mcid_text(
                            block[1],
                            font_code_maps=_page_font_code_maps(page),
                        ),
                        normalized_only=True,
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
                        if resolved is not None:
                            block = _get_mcid_block(resolved[1], mcid)
                            if block is not None and (
                                _spoken_list_label_text(
                                    _decode_label_mcid_text(
                                        block[1],
                                        font_code_maps=_page_font_code_maps(
                                            resolved[0]
                                        ),
                                    ),
                                    normalized_only=True,
                                )
                                is None
                            ):
                                # The repair leaves labels without a
                                # recognized normalization alone, so they
                                # are not "missing".
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


def _collect_struct_page_mcids(
    pdf: pikepdf.Pdf,
) -> tuple[set[tuple[tuple[int, int], int]], set[int]]:
    """Collect (page objgen, MCID) pairs owned by the structure tree.

    MCIDs are page-scoped integers, so ownership must be tested per page;
    a global MCID set masks orphans whenever another page's tree uses the
    same number. MCIDs whose owning page cannot be determined are returned
    separately and treated as owned everywhere.
    """
    struct_root = pdf.Root.get("/StructTreeRoot")
    owned: set[tuple[tuple[int, int], int]] = set()
    wildcard: set[int] = set()
    if struct_root is None:
        return owned, wildcard

    def record(mcid: int, page: pikepdf.Object | None) -> None:
        if page is None:
            wildcard.add(mcid)
        else:
            owned.add((page.objgen, mcid))

    def walk(obj: pikepdf.Dictionary, page: pikepdf.Object | None) -> None:
        own_page = obj.get("/Pg")
        if own_page is not None:
            page = own_page
        content = obj.get("/K")
        items: list[object]
        if isinstance(content, pikepdf.Array):
            items = list(content)
        elif content is None:
            items = []
        else:
            items = [content]
        for item in items:
            if isinstance(item, int):
                record(item, page)
            elif isinstance(item, pikepdf.Dictionary):
                if "/MCID" in item:
                    record(int(item.MCID), item.get("/Pg") or page)
                else:
                    walk(item, page)

    walk(struct_root, None)
    return owned, wildcard


_MC_STREAM_TOKEN = re.compile(
    rb"/(\w+)\s*(?:<<(?:(?!>>).)*>>)?\s*(?:BDC|BMC)"
    rb"|\bEMC\b"
    rb"|/([^\s/<>{}\[\]()]+)\s+Do\b",
    re.DOTALL,
)


def _artifact_drawn_xobject_names(data: bytes) -> set[bytes]:
    """Names of XObjects a page also draws inside /Artifact marked content."""
    names: set[bytes] = set()
    stack: list[bytes] = []
    artifact_depth = 0
    for match in _MC_STREAM_TOKEN.finditer(data):
        token = match.group(0)
        if token.endswith(b"BDC") or token.endswith(b"BMC"):
            tag = match.group(1)
            stack.append(tag)
            if tag == b"Artifact":
                artifact_depth += 1
        elif token == b"EMC":
            if stack and stack.pop() == b"Artifact":
                artifact_depth -= 1
        elif artifact_depth > 0:
            names.add(match.group(2))
    return names


def _body_has_show_ops(body: bytes) -> bool:
    return bool(
        _SIMPLE_SHOW_OP.search(body.decode("latin-1"))
        or _ARRAY_SHOW_OP.search(body.decode("latin-1"))
    )


def _body_xobject_names(body: bytes) -> set[bytes]:
    return set(re.findall(rb"/([^\s/<>{}\[\]()]+)\s+Do\b", body))


def _strip_content_strings(body: bytes) -> bytes:
    return re.sub(
        rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>",
        b"",
        body,
    )


def _body_is_decorative_paths(body: bytes) -> bool:
    """True when a marked block draws only vector paths: no text, no images."""
    if _mcid_body_has_image(body):
        return False
    stripped = _strip_content_strings(body)
    if re.search(rb"\bBT\b", stripped):
        return False
    return bool(re.search(rb"\b(?:re|m)\b", stripped))


def _body_paints_no_content(
    body: bytes,
    *,
    font_code_maps: dict[str, FontCodeMap] | None = None,
    initial_font: str | None = None,
) -> bool:
    """True when a block draws no image and shows nothing but whitespace."""
    if _mcid_body_has_image(body):
        return False
    decoded = _decode_shown_text_in_order(
        body, font_code_maps=font_code_maps, initial_font=initial_font
    )
    return decoded is not None and not decoded.strip()


def _orphan_marked_spoken_text(
    tag: str,
    body: bytes,
    mcid: int,
    *,
    table_image_labels: dict[int, str] | None = None,
    font_code_maps: dict[str, FontCodeMap] | None = None,
    initial_font: str | None = None,
) -> str | None:
    # Block-level /ActualText silences every glyph in the block, so the
    # spoken text must decode ALL of them; bail out when any show op is
    # unreadable rather than replace real spoken text with a fragment.
    decoded = _decode_shown_text_in_order(
        body, font_code_maps=font_code_maps, initial_font=initial_font
    )
    if decoded is None:
        return None
    text = re.sub(r"\s+", " ", decoded).strip()
    if tag == "Span":
        if not text and not _mcid_body_has_image(body):
            # A Span showing nothing but spaces paints no content. An empty
            # decode is the answer box for a /Lbl, where an empty label IS
            # the box, but here it would speak a word over a page that shows
            # none: the caller retags these /Artifact instead.
            return None
        return _spoken_list_label_text(text)
    if tag != "Table":
        return None
    if _mcid_body_has_image(body):
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
    *,
    font_code_maps: dict[str, FontCodeMap] | None = None,
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
        decoded = _decode_label_mcid_text(
            block[1], font_code_maps=font_code_maps
        )
        if decoded and not re.fullmatch(r"_+", decoded.strip()):
            continue
        for next_mcid, next_tag in orphans[index + 1 :]:
            if next_tag != "Span":
                continue
            next_block = _get_mcid_block(data, next_mcid)
            if next_block is None:
                continue
            spoken = _spoken_list_label_text(
                _decode_label_mcid_text(
                    next_block[1], font_code_maps=font_code_maps
                )
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
    owned_page_mcids, wildcard_mcids = _collect_struct_page_mcids(pdf)
    updated = 0

    for page in pdf.pages:
        data = _page_contents_data(page)
        if data is None:
            continue
        page_owned = {
            mcid for pg, mcid in owned_page_mcids if pg == page.obj.objgen
        } | wildcard_mcids
        artifact_names = _artifact_drawn_xobject_names(data)
        font_code_maps = _page_font_code_maps(page)
        table_image_labels = _pair_orphan_table_image_labels(
            data, struct_mcids, font_code_maps=font_code_maps
        )
        page_changed = False
        new_data = data
        for mcid, tag in _iter_bdc_mcids_on_page(data):
            if mcid in page_owned:
                continue
            if tag not in ("Table", "Span", "Figure"):
                continue
            if _mcid_bdc_has_actualtext(new_data, mcid):
                continue
            if tag == "Figure":
                block = _get_mcid_block_to_emc(new_data, mcid)
                if block is None:
                    continue
                body = block[2]
                paints_nothing = _body_paints_no_content(
                    body,
                    font_code_maps=font_code_maps,
                    initial_font=_font_in_effect_at(new_data, block[0]),
                )
                if _body_has_show_ops(body) and not paints_nothing:
                    # An orphan Figure that shows text needs a tree repair a
                    # sweep cannot infer; leave it for review, never silence it.
                    continue
                draw_names = _body_xobject_names(body)
                if draw_names and draw_names <= artifact_names or (
                    not draw_names
                    and (_body_is_decorative_paths(body) or paints_nothing)
                ):
                    # The page draws the same XObject again as an /Artifact
                    # (Word exports draw one rasterized graphics layer once
                    # per panel), so the producer already reads this repeat
                    # as decoration.
                    new_data, changed = _retag_orphan_bdc_as_artifact(
                        new_data, mcid
                    )
                    if changed:
                        page_changed = True
                        updated += 1
                        actions.append(
                            f"orphan MCID {mcid}: retagged decorative Figure to /Artifact"
                        )
                elif draw_names:
                    new_data, changed = _inject_actualtext_in_data(
                        new_data,
                        mcid=mcid,
                        actual_text=_UNTAGGED_IMAGE_FALLBACK_ACTUALTEXT,
                        preferred_tag=b"/Figure",
                    )
                    if changed:
                        page_changed = True
                        updated += 1
                        actions.append(
                            f"orphan MCID {mcid}: injected /ActualText "
                            f"{_UNTAGGED_IMAGE_FALLBACK_ACTUALTEXT!r} on Figure"
                        )
                continue
            # Use the block's full extent (to its own EMC, past inner ETs):
            # /ActualText covers all of it, so every decision below must see
            # every operator it silences.
            block = _get_mcid_block_to_emc(new_data, mcid)
            if block is None:
                continue
            body = block[2]
            spoken = _orphan_marked_spoken_text(
                tag,
                body,
                mcid,
                table_image_labels=table_image_labels,
                font_code_maps=font_code_maps,
                initial_font=_font_in_effect_at(new_data, block[0]),
            )
            if spoken is None and tag == "Table" and _table_body_has_paths_only(body):
                new_data, changed = _retag_orphan_bdc_as_artifact(new_data, mcid)
                if changed:
                    page_changed = True
                    updated += 1
                    actions.append(
                        f"orphan MCID {mcid}: retagged Table to /Artifact"
                    )
                continue
            # Word draws table borders and cell shading as `re f*` fills in
            # their own /Table blocks, and pads empty cells with one space;
            # neither paints anything a reader could speak.
            if not spoken and (
                _body_is_decorative_paths(body)
                or _body_paints_no_content(
                    body,
                    font_code_maps=font_code_maps,
                    initial_font=_font_in_effect_at(new_data, block[0]),
                )
            ):
                new_data, changed = _retag_orphan_bdc_as_artifact(new_data, mcid)
                if changed:
                    page_changed = True
                    updated += 1
                    actions.append(
                        f"orphan MCID {mcid}: retagged decorative {tag} to /Artifact"
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
        owned_page_mcids, wildcard_mcids = _collect_struct_page_mcids(pdf)
        for page in pdf.pages:
            data = _page_contents_data(page)
            if data is None:
                continue
            page_owned = {
                mcid for pg, mcid in owned_page_mcids if pg == page.obj.objgen
            } | wildcard_mcids
            for mcid, tag in _iter_bdc_mcids_on_page(data):
                if mcid in page_owned:
                    continue
                if tag not in ("Table", "Span", "Figure"):
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


def _struct_has_page_content(obj: pikepdf.Dictionary) -> bool:
    kids = obj.get("/K")
    items: list[pikepdf.Object] = []
    if isinstance(kids, pikepdf.Array):
        for kid in kids:
            if isinstance(kid, pikepdf.Array):
                items.extend(kid)
            else:
                items.append(kid)
    elif kids is not None:
        items.append(kids)
    for item in items:
        if not isinstance(item, pikepdf.Dictionary):
            return True
        if "/MCID" in item or item.get("/Type") == "/OBJR":
            return True
        if item.get("/S") is not None and _struct_has_page_content(item):
            return True
    return False


def _repair_contentless_alt(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Drop /Alt and /ActualText from struct elements that own no page content.

    Word leaves empty /Span elements with a single-space /ActualText inside
    text boxes. Acrobat fails them under "Alternate Text: Associated with
    content" because the text replaces nothing on any page.
    """
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0

    updated = 0

    def walk(obj: pikepdf.Dictionary) -> None:
        nonlocal updated
        for child in _struct_child_dicts(obj):
            if child.get("/S") is None:
                continue
            keys = [key for key in ("/Alt", "/ActualText") if key in child]
            if keys and not _struct_has_page_content(child):
                for key in keys:
                    del child[key]
                updated += 1
                actions.append(
                    f"removed {'/'.join(k.lstrip('/') for k in keys)} from empty "
                    f"{str(child['/S']).lstrip('/')} with no page content"
                )
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


def _retag_orphan_bdc_as_artifact(
    data: bytes,
    mcid: int,
) -> tuple[bytes, bool]:
    """Retag one orphan marked block as /Artifact, dropping its dead MCID."""
    pattern = re.compile(
        rb"/(?P<tag>" + _MCID_BDC_TAG + rb")\s*<<"
        rb"((?:(?!>>).)*?/MCID\s+"
        + _mcid_token(mcid)
        + rb"(?:(?!>>).)*?)>>\s*BDC",
        re.DOTALL,
    )

    def repl(match: re.Match[bytes]) -> bytes:
        props = re.sub(
            rb"/MCID\s+\d+(?!\d)",
            b"",
            match.group(2),
        ).strip()
        if not props:
            return b"/Artifact BMC"
        return b"/Artifact<< " + props + b" >> BDC"

    new_data, count = pattern.subn(repl, data, count=1)
    return new_data, count > 0


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
    """Convert wholly non-image Figures without orphaning their tagged content."""
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
        if keep:
            # Mixed image/text ownership is ambiguous. Splitting it would also
            # require rewriting ParentTree ownership, so preserve it unchanged.
            continue

        new_data = data
        data_changed = False
        for mcid in drop:
            block = _get_mcid_block(new_data, mcid)
            if block is not None and block[0] == "Figure":
                new_data, changed = _retag_mcid_bdc_in_data(new_data, mcid, b"Span")
                data_changed = data_changed or changed

        if data_changed:
            figure["/S"] = pikepdf.Name("/Span")
            if figure.get("/Alt") is not None:
                del figure["/Alt"]
            if figure.get("/Contents") is not None:
                del figure["/Contents"]
            updated += 1
            actions.append(
                f"converted non-image Figure to /Span for MCIDs {drop}"
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
    actual = re.search(
        rb"/ActualText\s+((?:\((?:\\.|[^\\()])*\))|(?:<[0-9A-Fa-f\s]*>))",
        match.group(2),
    )
    if actual is None:
        return None
    return _decode_pdf_actualtext_value(actual.group(1))


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


def _decode_pdf_actualtext_value(value: bytes) -> str:
    """Decode a PDF literal or hex string ActualText value."""
    if value.startswith(b"<"):
        hex_digits = re.sub(rb"\s+", b"", value[1:-1])
        if len(hex_digits) % 2:
            hex_digits += b"0"
        try:
            raw = bytes.fromhex(hex_digits.decode("ascii"))
        except ValueError:
            return ""
        if raw.startswith(b"\xfe\xff"):
            return raw[2:].decode("utf-16-be", errors="replace")
        return raw.decode("latin1", errors="replace")
    return _decode_pdf_literal(value)


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


def _alt_precedence_protected_mcids(
    pdf: pikepdf.Pdf,
) -> set[tuple[tuple[int, int], int]]:
    """(page objgen, MCID) pairs whose content /ActualText the alt-precedence
    repair strips: MCIDs claimed by a Figure keeping an authoritative struct
    /Alt. Injecting /ActualText on these elsewhere is undone in the same pass,
    so writers must skip them or the output never reaches a byte-stable state.
    """
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return set()

    protected: set[tuple[tuple[int, int], int]] = set()
    figures: list[pikepdf.Dictionary] = []

    def collect(obj: pikepdf.Dictionary) -> None:
        if obj.get("/S") == "/Figure":
            figures.append(obj)
        for child in _struct_child_dicts(obj):
            collect(child)

    collect(struct_root)

    page_cache: dict[tuple[int, int], bytes] = {}
    for figure in figures:
        if figure.get("/Alt") is None:
            continue
        classes = struct_class_names(figure)
        if "inlineFormula" in classes or any(
            "inlineFormula" in name for name in classes
        ):
            continue
        alt_text = _normalize_figure_alt_text(figure.get("/Alt"))
        if looks_like_table_figure_alt(alt_text) or any(
            "table-figure-reverted" in name for name in classes
        ):
            continue
        page = _resolve_struct_page(pdf, figure)
        if page is None:
            continue
        key = page.objgen
        if key not in page_cache:
            contents = page.get("/Contents")
            page_cache[key] = (
                _read_page_contents(contents) if contents is not None else b""
            )
        data = page_cache[key]
        if not data:
            continue
        for mcid in _figure_content_mcids(data, _collect_mcids(figure.get("/K"))):
            protected.add((key, mcid))
    return protected


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
    if any(_is_private_use(char) for char in actual_text):
        # Spoken text is derived from font evidence; a Private Use codepoint
        # means there is none for this glyph, so nothing may be written.
        return data, False
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


_UNTAGGED_IMAGE_FALLBACK_ACTUALTEXT = "Figure"

# OCR needs glyphs big enough to read: a placement narrower than this in
# either dimension is a mark (tick, arrow, rule), not a readable image.
_MIN_OCR_IMAGE_DIMENSION_PT = 9.0


def _element_has_own_alt(obj: pikepdf.Dictionary) -> bool:
    for key in ("/Alt", "/ActualText"):
        value = obj.get(key)
        if value is not None and str(value).strip():
            return True
    return False


def _page_image_names(page: pikepdf.Page | pikepdf.Dictionary) -> set[str]:
    resources = page.get("/Resources")
    if not isinstance(resources, pikepdf.Dictionary):
        return set()
    xobjects = resources.get("/XObject")
    if not isinstance(xobjects, pikepdf.Dictionary):
        return set()
    names: set[str] = set()
    for name, candidate in xobjects.items():
        subtype = candidate.get("/Subtype") if hasattr(candidate, "get") else None
        if subtype == "/Image":
            names.add(str(name).lstrip("/"))
    return names


def _mcid_image_xobject_names(body: bytes) -> list[str]:
    return [
        match.group(1).decode("latin1")
        for match in re.finditer(rb"/([^\s/<>{}\[\]()]+)\s+Do\b", body)
    ]


_MARKED_CONTENT_TOKEN = re.compile(rb"\b(BDC|BMC|EMC)\b")


def _get_mcid_block_to_emc(data: bytes, mcid: int) -> tuple[int, int, bytes] | None:
    """Return the (start, end, body) of an MCID block bounded by its own EMC.

    Unlike _get_mcid_block, this does not stop at an inner ET or at a nested
    marked-content EMC, so mixed text-and-image bodies keep every operator.
    """
    open_pattern = re.compile(
        rb"/(?P<tag>" + _MCID_BDC_TAG + rb")\s*<<"
        rb"(?:(?!>>).)*?/MCID\s+"
        + _mcid_token(mcid)
        + rb"(?:(?!>>).)*?>>\s*BDC",
        re.DOTALL,
    )
    match = open_pattern.search(data)
    if match is None:
        return None
    depth = 1
    for token in _MARKED_CONTENT_TOKEN.finditer(data, match.end()):
        if token.group(1) in (b"BDC", b"BMC"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                start = match.end()
                return start, token.start(), data[start : token.start()]
    return None


def _ocr_page_clip_text(fitz_page: pymupdf.Page, rect: pymupdf.Rect) -> str:
    try:
        pixmap = fitz_page.get_pixmap(dpi=200, clip=rect)
        ocr_doc = pymupdf.open(
            stream=pixmap.pdfocr_tobytes(language="eng", compress=True),
            filetype="pdf",
        )
        text = ocr_doc[0].get_text()
        ocr_doc.close()
    except (RuntimeError, ValueError, IndexError):
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip("|").strip()


_OCR_COMMON_SHORT_WORDS = frozenset(
    "am an as at be by do go he if in is it me my no of on or so to up us we".split()
)


def _ocr_text_is_reliable(text: str) -> bool:
    """Reject garbled OCR (chemical structures, stylized infographics).

    OCR of a diagram with no running text yields stray letter pairs,
    symbols, and mangled fragments ("=, R Be - SS HO 'S", "Oo io) |oO");
    injecting that as ActualText is worse than the generic fallback. Count
    junk tokens: bare symbols, tokens with characters OCR shouldn't emit
    mid-word, and two-letter non-words; require the junk share to stay
    strictly under 20% (garbled short captions land exactly on it).
    """
    tokens = text.split()
    if not tokens or not re.search(r"[A-Za-z]{3,}", text):
        return False
    junk = 0
    for token in tokens:
        stripped = token.strip(".,;:!?()[]'\"")
        letters = re.sub(r"[^A-Za-z]", "", stripped)
        if not re.search(r"[A-Za-z0-9]", token):
            junk += 1
        elif re.search(r"[^A-Za-z0-9.,;:!?()\[\]'\"%/&+=-]", token):
            junk += 1
        elif (
            len(letters) == 2
            and letters.lower() not in _OCR_COMMON_SHORT_WORDS
            and not re.search(r"\d", stripped)
        ):
            junk += 1
    return junk / len(tokens) < 0.2


def _wrap_image_do_with_actualtext(
    data: bytes,
    *,
    mcid: int,
    image_texts: dict[str, str],
) -> tuple[bytes, bool]:
    block = _get_mcid_block_to_emc(data, mcid)
    if block is None:
        return data, False
    start, end, body = block
    changed = False
    for name, text in image_texts.items():
        do_pattern = re.compile(
            rb"/" + re.escape(name.encode("latin1")) + rb"\s+Do\b(?!\s*EMC)"
        )

        def wrap(do_match: re.Match[bytes]) -> bytes:
            return (
                b"/Span << /ActualText "
                + _pdf_literal_string(text)
                + b" >> BDC "
                + do_match.group(0)
                + b" EMC"
            )

        body, count = do_pattern.subn(wrap, body)
        changed = changed or count > 0
    if not changed:
        return data, False
    return data[:start] + body + data[end:], True


def _unwrapped_image_names(body: bytes, image_names: set[str]) -> list[str]:
    return [
        name
        for name in dict.fromkeys(_mcid_image_xobject_names(body))
        if name in image_names
        and re.search(
            rb"/" + re.escape(name.encode("latin1")) + rb"\s+Do\b(?!\s*EMC)",
            body,
        )
    ]


def _apply_untagged_image_texts(
    data: bytes,
    *,
    mcid: int,
    body: bytes,
    image_texts: dict[str, str],
) -> tuple[bytes, bool]:
    if _is_image_only_mcid_body(body):
        combined = " ".join(dict.fromkeys(image_texts.values()))
        return _inject_actualtext_in_data(data, mcid=mcid, actual_text=combined)
    return _wrap_image_do_with_actualtext(data, mcid=mcid, image_texts=image_texts)


def _struct_owned_mcid_keys(
    pdf: pikepdf.Pdf,
) -> tuple[set[tuple[tuple[int, int], int]], set[int]]:
    """Map struct-referenced MCIDs to their pages; page-less MCIDs shield all pages."""
    owned: set[tuple[tuple[int, int], int]] = set()
    unpaged: set[int] = set()
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return owned, unpaged

    def nearest_page(obj: pikepdf.Dictionary) -> pikepdf.Dictionary | None:
        page = obj.get("/Pg")
        parent = obj.get("/P")
        while page is None and isinstance(parent, pikepdf.Dictionary):
            page = parent.get("/Pg")
            parent = parent.get("/P")
        return page

    def record(mcid: int, page: pikepdf.Dictionary | None) -> None:
        if page is None:
            unpaged.add(mcid)
        else:
            owned.add((page.objgen, mcid))

    def walk(obj: pikepdf.Dictionary) -> None:
        page = nearest_page(obj)
        content = obj.get("/K")
        items = list(content) if isinstance(content, pikepdf.Array) else [content]
        for item in items:
            if isinstance(item, pikepdf.Dictionary):
                mcid = item.get("/MCID")
                if mcid is not None:
                    record(int(mcid), item.get("/Pg") or page)
                else:
                    walk(item)
            else:
                try:
                    record(int(item), page)
                except (TypeError, ValueError):
                    continue

    walk(struct_root)
    return owned, unpaged


def _repair_untagged_image_actualtext(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Add /ActualText for image content owned by non-Figure elements without alt."""
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0

    targets: list[pikepdf.Dictionary] = []

    def collect(obj: pikepdf.Dictionary, covered: bool) -> None:
        tag = obj.get("/S")
        if tag is not None:
            covered = covered or _element_has_own_alt(obj)
            if tag == "/Figure":
                covered = True
            elif not covered and _collect_mcids(obj.get("/K")):
                targets.append(obj)
        for child in _struct_child_dicts(obj):
            collect(child, covered)

    collect(struct_root, False)

    page_indices = {page.objgen: index for index, page in enumerate(pdf.pages)}
    page_states: dict[tuple[int, int], tuple[pikepdf.Page, bytes]] = {}
    dirty_pages: set[tuple[int, int]] = set()
    fitz_doc: pymupdf.Document | None = None
    ocr_cache: dict[tuple[tuple[int, int], str], str] = {}
    updated = 0

    def load_page(page: pikepdf.Page) -> tuple[pikepdf.Page, bytes]:
        page_key = page.objgen
        if page_key not in page_states:
            contents = page.get("/Contents")
            page_states[page_key] = (
                page,
                _read_page_contents(contents) if contents is not None else b"",
            )
        return page_states[page_key]

    def ocr_image_texts(
        page_key: tuple[int, int],
        names: list[str],
    ) -> dict[str, str]:
        nonlocal fitz_doc
        if fitz_doc is None:
            buffer = io.BytesIO()
            pdf.save(buffer)
            fitz_doc = pymupdf.open(stream=buffer.getvalue(), filetype="pdf")
        page_index = page_indices.get(page_key)
        image_texts: dict[str, str] = {}
        for name in names:
            cache_key = (page_key, name)
            if cache_key not in ocr_cache:
                text = ""
                if page_index is not None:
                    fitz_page = fitz_doc[page_index]
                    rects = fitz_page.get_image_rects(name)
                    # A reused image name shares one ActualText across all
                    # its placements, and OCR of a placement only points
                    # tall (tick marks, arrows) yields fake words; only
                    # OCR when every placement is large enough to read.
                    if rects and all(
                        min(r.width, r.height) >= _MIN_OCR_IMAGE_DIMENSION_PT
                        for r in rects
                    ):
                        text = _ocr_page_clip_text(fitz_page, rects[0])
                if not _ocr_text_is_reliable(text):
                    text = ""
                ocr_cache[cache_key] = text
            image_texts[name] = (
                ocr_cache[cache_key] or _UNTAGGED_IMAGE_FALLBACK_ACTUALTEXT
            )
        return image_texts

    def repair_mcid(
        page_key: tuple[int, int],
        mcid: int,
        *,
        label: str,
    ) -> None:
        nonlocal updated
        page_ref, data = page_states[page_key]
        image_names = _page_image_names(page_ref)
        if not image_names:
            return
        block = _get_mcid_block_to_emc(data, mcid)
        if block is None:
            return
        body = block[2]
        names = _unwrapped_image_names(body, image_names)
        if not names or _mcid_bdc_has_actualtext(data, mcid):
            return
        image_texts = ocr_image_texts(page_key, names)
        data, changed = _apply_untagged_image_texts(
            data,
            mcid=mcid,
            body=body,
            image_texts=image_texts,
        )
        if changed:
            page_states[page_key] = (page_ref, data)
            dirty_pages.add(page_key)
            updated += 1
            actions.append(
                f"{label} {mcid}: added /ActualText to untagged image content"
            )

    for obj in targets:
        page = _resolve_struct_page(pdf, obj)
        if page is None:
            continue
        page_key = page.objgen
        _, data = load_page(page)
        if not data:
            continue
        for mcid in _collect_mcids(obj.get("/K")):
            repair_mcid(page_key, mcid, label="mcid")

    owned, unpaged = _struct_owned_mcid_keys(pdf)
    for page in pdf.pages:
        page_key = page.objgen
        _, data = load_page(page)
        if not data:
            continue
        for mcid, tag in _iter_bdc_mcids_on_page(data):
            if tag == "Artifact":
                continue
            if (page_key, mcid) in owned or mcid in unpaged:
                continue
            repair_mcid(page_key, mcid, label="orphan image MCID")

    for page_key in dirty_pages:
        page_ref, data = page_states[page_key]
        page_ref["/Contents"] = pdf.make_stream(data, compress=True)
    return updated


def list_untagged_image_mcids_missing_actualtext(pdf_bytes: bytes) -> list[str]:
    """Label image MCIDs owned by alt-less non-Figure elements, e.g. 'page6 mcid25'."""
    labels: list[str] = []
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            return labels

        targets: list[pikepdf.Dictionary] = []

        def collect(obj: pikepdf.Dictionary, covered: bool) -> None:
            tag = obj.get("/S")
            if tag is not None:
                covered = covered or _element_has_own_alt(obj)
                if tag == "/Figure":
                    covered = True
                elif not covered and _collect_mcids(obj.get("/K")):
                    targets.append(obj)
            for child in _struct_child_dicts(obj):
                collect(child, covered)

        collect(struct_root, False)

        page_indices = {page.objgen: index for index, page in enumerate(pdf.pages)}
        page_cache: dict[tuple[int, int], tuple[pikepdf.Page, bytes]] = {}
        seen: set[tuple[tuple[int, int], int]] = set()

        def flag_mcid(page_key: tuple[int, int], mcid: int, suffix: str) -> None:
            if (page_key, mcid) in seen:
                return
            page_ref, data = page_cache[page_key]
            image_names = _page_image_names(page_ref)
            if not image_names:
                return
            block = _get_mcid_block_to_emc(data, mcid)
            if block is None:
                return
            body = block[2]
            if _mcid_bdc_has_actualtext(data, mcid):
                return
            if not _unwrapped_image_names(body, image_names):
                return
            seen.add((page_key, mcid))
            page_number = page_indices.get(page_key)
            page_label = (
                f"page{page_number + 1}" if page_number is not None else "page?"
            )
            labels.append(f"{page_label} mcid{mcid}{suffix}")

        def load_page(page: pikepdf.Page) -> bytes:
            page_key = page.objgen
            if page_key not in page_cache:
                contents = page.get("/Contents")
                page_cache[page_key] = (
                    page,
                    _read_page_contents(contents) if contents is not None else b"",
                )
            return page_cache[page_key][1]

        for obj in targets:
            page = _resolve_struct_page(pdf, obj)
            if page is None:
                continue
            if not load_page(page):
                continue
            for mcid in _collect_mcids(obj.get("/K")):
                flag_mcid(page.objgen, mcid, "")

        owned, unpaged = _struct_owned_mcid_keys(pdf)
        for page in pdf.pages:
            page_key = page.objgen
            data = load_page(page)
            if not data:
                continue
            for mcid, tag in _iter_bdc_mcids_on_page(data):
                if tag == "Artifact":
                    continue
                if (page_key, mcid) in owned or mcid in unpaged:
                    continue
                flag_mcid(page_key, mcid, " (orphan)")
    return labels


def _reachable_struct_objgens(pdf: pikepdf.Pdf) -> set[tuple[int, int]]:
    """Object ids of every struct element reached from the StructTreeRoot."""
    struct_root = pdf.Root.get("/StructTreeRoot")
    reachable: set[tuple[int, int]] = set()
    if struct_root is None:
        return reachable

    def walk(obj: pikepdf.Dictionary) -> None:
        for child in _struct_child_dicts(obj):
            if "/MCID" in child or child.get("/Type") == "/OBJR":
                continue
            if child.is_indirect:
                if child.objgen in reachable:
                    continue
                reachable.add(child.objgen)
            walk(child)

    walk(struct_root)
    return reachable


def _struct_descends_from(
    obj: pikepdf.Dictionary,
    ancestor: pikepdf.Dictionary,
) -> bool:
    current: object = obj
    for _ in range(64):
        if not isinstance(current, pikepdf.Dictionary):
            return False
        if current.is_indirect and current.objgen == ancestor.objgen:
            return True
        current = current.get("/P")
    return False


def _page_named_for_descendants(
    pdf: pikepdf.Pdf,
    obj: pikepdf.Dictionary,
    mcids: list[int],
) -> pikepdf.Page | None:
    """The one page whose ParentTree names obj or its descendants for mcids."""
    matches: list[pikepdf.Page] = []
    for page in pdf.pages:
        named = 0
        for mcid in mcids:
            owner = _parent_tree_owner(pdf, page, mcid)
            if owner is None:
                continue
            if not _struct_descends_from(owner, obj):
                break
            named += 1
        else:
            if named:
                matches.append(page)
    return matches[0] if len(matches) == 1 else None


def _repair_pageless_alt_content_page(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Give a /Pg to an alternate-text element whose bare MCIDs name no page.

    An earlier round rewrote reverted tables as /Figure /Alt "Table" with bare
    MCIDs copied from the abandoned cell subtree and no /Pg anywhere up the
    chain. Acrobat cannot find the content that alternate text replaces and
    fails "Alternate text: Associated with content", and the sweeps here treat
    the MCIDs as owned on every page, which hides real orphans. The page whose
    ParentTree names this element's own descendants for those MCIDs is the
    file's statement of where the content is.
    """
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0
    page_numbers = {page.obj.objgen: index for index, page in enumerate(pdf.pages, 1)}
    updated = 0

    def walk(obj: pikepdf.Dictionary, page: pikepdf.Object | None) -> None:
        nonlocal updated
        own_page = obj.get("/Pg")
        if own_page is not None:
            page = own_page
        if (
            page is None
            and obj.is_indirect
            and obj.get("/S") is not None
            and ("/Alt" in obj or "/ActualText" in obj)
        ):
            mcids = _collect_mcids(obj.get("/K"))
            found = _page_named_for_descendants(pdf, obj, mcids) if mcids else None
            if found is not None:
                obj["/Pg"] = found.obj
                page = found.obj
                updated += 1
                actions.append(
                    f"set /Pg on pageless {str(obj['/S']).lstrip('/')} with "
                    f"alternate text to page {page_numbers[found.obj.objgen]} "
                    f"named by its ParentTree entries for MCIDs {mcids}"
                )
        for child in _struct_child_dicts(obj):
            if "/MCID" in child or child.get("/Type") == "/OBJR":
                continue
            walk(child, page)

    walk(struct_root, None)
    return updated


# Structure types whose content model takes a Figure. Grouping elements and
# block containers do; a list, a table, a row or another Figure does not.
_FIGURE_CONTAINERS = frozenset(
    {
        "/Document",
        "/DocumentFragment",
        "/Part",
        "/Art",
        "/Sect",
        "/Div",
        "/BlockQuote",
        "/Aside",
        "/Caption",
        "/LBody",
        "/TD",
        "/TH",
        "/P",
    }
)


def _reachable_container_for(
    obj: pikepdf.Dictionary,
    reachable: set[tuple[int, int]],
) -> tuple[pikepdf.Dictionary, pikepdf.Dictionary] | None:
    """Nearest reachable ancestor that takes a Figure and speaks no alt itself.

    Returns that container and the child on the /P chain directly under it,
    which is where the element used to sit and where it goes back in.
    """
    child = obj
    parent = child.get("/P")
    for _ in range(64):
        if not isinstance(parent, pikepdf.Dictionary):
            return None
        if parent.get("/Type") == "/StructTreeRoot":
            return None
        if (
            parent.is_indirect
            and parent.objgen in reachable
            and str(parent.get("/S")) in _FIGURE_CONTAINERS
            and "/Alt" not in parent
            and "/ActualText" not in parent
        ):
            return parent, child
        child = parent
        parent = parent.get("/P")
    return None


def _struct_kids_list(obj: pikepdf.Dictionary) -> list[object]:
    kids = obj.get("/K")
    if isinstance(kids, pikepdf.Array):
        return list(kids)
    if kids is None:
        return []
    return [kids]


_LABEL_ONLY_ALT = re.compile(
    r"^(?:figure|fig\.?|table|diagram|image|graph|chart|equation)\s*\d*\.?$",
    re.IGNORECASE,
)


def _figure_alt_names_no_content(alt_text: str, figure: pikepdf.Dictionary) -> bool:
    """True for an Alt that labels the figure without describing it.

    Covers a bare "Figure 24" and the caption-only alt the inline-formula
    expander pads with its "Spoken formula notation" sentence.
    """
    if _LABEL_ONLY_ALT.match(alt_text.strip()):
        return True
    return bool(
        classify_figure_alt(alt_text, struct_classes=struct_class_names(figure))
    )


def _repair_dead_alt_figure_owners(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Reattach an unreachable /Figure with /Alt named for orphan Figure blocks.

    Word tags each equation fragment as its own /Figure block and Adobe's
    tagger gives the Figure element an /Alt spelling the equation out. An
    earlier round reverted the enclosing Table to a Figure and abandoned its
    TR/TD subtree, so the equation Figure is still named by the ParentTree but
    no longer reached from the root; its blocks read as orphans, and an orphan
    Figure that shows text is left alone. Hang the Figure back on the nearest
    reachable ancestor that takes a Figure and carries no alternate text of
    its own, right after the reachable child it used to sit under.
    """
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0
    reachable = _reachable_struct_objgens(pdf)
    owned_page_mcids, wildcard_mcids = _collect_struct_page_mcids(pdf)
    updated = 0

    for index, page in enumerate(pdf.pages, 1):
        data = _page_contents_data(page)
        if data is None:
            continue
        page_key = page.obj.objgen
        page_owned = {mcid for pg, mcid in owned_page_mcids if pg == page_key}
        for mcid, tag in _iter_bdc_mcids_on_page(data):
            if tag != "Figure" or mcid in page_owned or mcid in wildcard_mcids:
                continue
            owner = _parent_tree_owner(pdf, page, mcid)
            if owner is None or not owner.is_indirect or owner.objgen in reachable:
                continue
            alt_text = _normalize_figure_alt_text(owner.get("/Alt"))
            if owner.get("/S") != "/Figure" or not alt_text:
                continue
            if _figure_alt_names_no_content(alt_text, owner):
                # Once reattached, the Alt is what a reader hears in place of
                # the blocks, and the inline-formula walk stamps it over the
                # first one. A label or caption would silence the equation.
                continue
            owner_page = owner.get("/Pg")
            if owner_page is not None and owner_page.objgen != page_key:
                continue
            mcids = _collect_mcids(owner.get("/K"))
            if mcid not in mcids or any(other in page_owned for other in mcids):
                continue
            placement = _reachable_container_for(owner, reachable)
            if placement is None:
                continue
            container, anchor = placement
            kids = _struct_kids_list(container)
            insert_at = next(
                (
                    position + 1
                    for position, kid in enumerate(kids)
                    if isinstance(kid, pikepdf.Dictionary)
                    and kid.is_indirect
                    and kid.objgen == anchor.objgen
                ),
                len(kids),
            )
            kids.insert(insert_at, owner)
            container["/K"] = pikepdf.Array(kids)
            owner["/P"] = container
            _set_struct_page_if_missing(owner, page)
            reachable.add(owner.objgen)
            page_owned.update(mcids)
            updated += 1
            actions.append(
                f"page {index}: reattached unreachable Figure {alt_text[:40]!r} "
                f"owning orphan MCIDs {mcids} under "
                f"{str(container['/S']).lstrip('/')}"
            )
    return updated


def _struct_layout_bbox(
    obj: pikepdf.Dictionary,
) -> tuple[float, float, float, float] | None:
    attributes = obj.get("/A")
    candidates: list[object]
    if isinstance(attributes, pikepdf.Array):
        candidates = list(attributes)
    else:
        candidates = [attributes]
    for candidate in candidates:
        if not isinstance(candidate, pikepdf.Dictionary):
            continue
        bbox = candidate.get("/BBox")
        if isinstance(bbox, pikepdf.Array) and len(bbox) == 4:
            x0, y0, x1, y1 = (float(value) for value in bbox)
            return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    return None


_TM_OPERATOR = re.compile(
    rb"(?:[-+]?[\d.]+\s+){4}([-+]?[\d.]+)\s+([-+]?[\d.]+)\s+Tm\b"
)
_TEXT_MOVE_OPERATOR = re.compile(rb"\b(?:cm|Td|TD|T\*)\b|(?<=\))\s*['\"]")


def _text_origins(body: bytes) -> list[tuple[float, float]]:
    """Page-space text origins of a block, or [] when they cannot be read off.

    Only `Tm` sets an absolute origin; a `cm`, a `Td` or a line advance moves
    text somewhere the matrix operands do not say, so such a block is not
    placed.
    """
    if _TEXT_MOVE_OPERATOR.search(_strip_content_strings(body)):
        return []
    return [
        (float(match.group(1)), float(match.group(2)))
        for match in _TM_OPERATOR.finditer(body)
    ]


def _repair_parent_tree_named_figure_content(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Link an orphan block into the reachable /Figure its ParentTree entry names.

    Adobe's tagger left the Figure's /K naming only its image block while the
    page's ParentTree names the Figure for the equation text drawn inside the
    Figure's layout /BBox as well. The struct /Alt already speaks that text;
    without the link the block is an orphan whose only sweep repair would be
    an /ActualText built from glyphs the font may not name.
    """
    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return 0
    reachable = _reachable_struct_objgens(pdf)
    owned_page_mcids, wildcard_mcids = _collect_struct_page_mcids(pdf)
    updated = 0

    for index, page in enumerate(pdf.pages, 1):
        data = _page_contents_data(page)
        if data is None:
            continue
        page_key = page.obj.objgen
        page_owned = {
            mcid for pg, mcid in owned_page_mcids if pg == page_key
        } | wildcard_mcids
        for mcid, _tag in _iter_bdc_mcids_on_page(data):
            if mcid in page_owned:
                continue
            owner = _parent_tree_owner(pdf, page, mcid)
            if owner is None or not owner.is_indirect or owner.objgen not in reachable:
                continue
            alt_text = _normalize_figure_alt_text(owner.get("/Alt"))
            if owner.get("/S") != "/Figure" or not alt_text:
                continue
            owner_page = owner.get("/Pg")
            if owner_page is None or owner_page.objgen != page_key:
                continue
            bbox = _struct_layout_bbox(owner)
            if bbox is None:
                continue
            block = _get_mcid_block_to_emc(data, mcid)
            if block is None or not _body_has_show_ops(block[2]):
                continue
            origins = _text_origins(block[2])
            x0, y0, x1, y1 = bbox
            if not origins or not all(
                x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1 for x, y in origins
            ):
                continue
            kids = _struct_kids_list(owner)
            insert_at = next(
                (
                    position
                    for position, kid in enumerate(kids)
                    if isinstance(kid, int) and int(kid) > mcid
                ),
                len(kids),
            )
            kids.insert(insert_at, mcid)
            owner["/K"] = pikepdf.Array(kids)
            page_owned.add(mcid)
            updated += 1
            actions.append(
                f"page {index}: linked orphan MCID {mcid} into the Figure "
                f"{alt_text[:40]!r} its ParentTree entry names"
            )
    return updated


def _struct_page_ref(page: pikepdf.Page | pikepdf.Dictionary) -> pikepdf.Dictionary:
    """Return a page dictionary suitable for struct-tree /Pg entries."""
    if isinstance(page, pikepdf.Page):
        return page.obj
    return page


def _set_struct_page_if_missing(
    obj: pikepdf.Dictionary,
    page: pikepdf.Page | pikepdf.Dictionary,
) -> None:
    if obj.get("/Pg") is None:
        obj["/Pg"] = _struct_page_ref(page)


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

        _repair_pageless_alt_content_page(pdf, actions=actions)
        _repair_dead_alt_figure_owners(pdf, actions=actions)
        _repair_parent_tree_named_figure_content(pdf, actions=actions)
        figure_index = 0
        protected_mcids = _alt_precedence_protected_mcids(pdf)

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
                        page_key = page.objgen
                        mcid_texts = {
                            mcid: text
                            for mcid, text in mcid_texts.items()
                            if (page_key, mcid) not in protected_mcids
                        }
                        if not has_inline_formula and alt_text == "Table":
                            # The placeholder alt names no content, so over a
                            # block that shows text it would speak "Table" in
                            # place of the text.
                            data = _page_contents_data(page) or b""
                            mcid_texts = {
                                mcid: text
                                for mcid, text in mcid_texts.items()
                                if not _mcid_block_shows_text(data, mcid)
                            }
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
                            if (page.objgen, mcid) in protected_mcids:
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
        _repair_contentless_alt(pdf, actions=actions)
        _repair_extra_char_span_nested_alt(pdf, actions=actions)
        mcids_updated += _repair_list_image_labels(pdf, actions=actions)
        mcids_updated += _repair_list_item_label_actualtext(pdf, actions=actions)
        _repair_figure_mcid_linkage_and_tags(pdf, actions=actions)
        mcids_updated += _repair_orphan_marked_content_actualtext(pdf, actions=actions)
        mcids_updated += _repair_untagged_image_actualtext(pdf, actions=actions)
        _repair_figure_alt_precedence(pdf, actions=actions)
        _repair_figure_duplicate_contents(pdf, actions=actions)

        if not actions:
            result = MarkedContentActualTextRepairResult(
                figures_found=figures_found,
                mcids_updated=mcids_updated,
                actions=actions,
            )
            return pdf_bytes, result
        output = io.BytesIO()
        pdf.save(output)
        result = MarkedContentActualTextRepairResult(
            figures_found=figures_found,
            mcids_updated=mcids_updated,
            actions=actions,
        )
        return output.getvalue(), result
