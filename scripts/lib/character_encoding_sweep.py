"""Repair Acrobat character-encoding failures for symbol glyphs and ambiguous CMaps."""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field

import pikepdf
import pymupdf

from lib.marked_content_actualtext_sweep import (
    _get_mcid_block_to_emc,
    _inject_actualtext_on_page,
    _mcid_bdc_has_actualtext,
    _pdf_literal_string,
    _read_page_contents,
    _resolve_struct_page,
    _struct_child_dicts,
    _struct_element_mcids,
)

_STRUCT_TAGS = frozenset(
    {
        "/Span",
        "/Lbl",
        "/P",
        "/StyleSpan",
        "/ParagraphSpan",
        "/H1",
        "/H2",
        "/H3",
        "/LBody",
        "/Link",
    }
)

_SYMBOL_CHARS = frozenset("□●αβ\ufffd")
_SYMBOL_SPOKEN = {
    "□": "blank",
    "●": "bullet",
    "\ufffd": "",
}

_BFCHAR_SECTION = re.compile(r"(?:\d+\s+)?beginbfchar\s*(.*?)endbfchar", re.DOTALL)
_BFRANGE_SECTION = re.compile(r"(?:\d+\s+)?beginbfrange\s*(.*?)endbfrange", re.DOTALL)
_CMAP_SECTION = re.compile(
    r"(?:(?P<count>\d+)\s+)?begin(?P<kind>bfchar|bfrange)"
    r"\s*(?P<body>.*?)end(?P=kind)",
    re.DOTALL,
)
_HEX_TOKEN = re.compile(r"<([0-9A-Fa-f]+)>")
_BFRANGE_ENTRY = re.compile(
    r"<([0-9A-Fa-f]+)>\s+<([0-9A-Fa-f]+)>\s+"
    r"(?:<([0-9A-Fa-f]+)>|\[((?:[^\]]|\](?!\s*(?:<|$)))*)\])",
    re.DOTALL,
)
_CONTENT_MCID_PATTERN = re.compile(rb"/MCID\s+(\d+)(?!\d)")


def _parse_bfchar_pairs(data: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for match in _BFCHAR_SECTION.finditer(data):
        section = match.group(1)
        pairs.extend(re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", section))
    return pairs


def _valid_source_range(start: str, end: str) -> bool:
    return (
        bool(start)
        and bool(end)
        and len(start) == len(end)
        and len(start) <= 4
        and len(start) % 2 == 0
        and int(start, 16) <= int(end, 16)
    )


def _strict_unicode_from_hex(dst: str) -> str | None:
    if not dst or len(dst) % 4 != 0:
        return None
    try:
        text = bytes.fromhex(dst).decode("utf-16-be", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    if not text or any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        return None
    return text


def _parse_bfrange_pairs(data: str) -> list[tuple[str, str]]:
    """Expand only bfranges whose source and destination are unambiguous."""
    pairs: list[tuple[str, str]] = []
    for match in _BFRANGE_SECTION.finditer(data):
        section = match.group(1)
        for entry in _BFRANGE_ENTRY.finditer(section):
            start, end, scalar, array_body = entry.groups()
            if not _valid_source_range(start, end):
                continue
            first = int(start, 16)
            last = int(end, 16)
            count = last - first + 1
            if scalar is not None:
                if len(scalar) != 4:
                    continue
                scalar_value = int(scalar, 16)
                last_value = scalar_value + count - 1
                if (
                    0xD800 <= scalar_value <= 0xDFFF
                    or 0xD800 <= last_value <= 0xDFFF
                    or last_value > 0xFFFF
                ):
                    continue
                for offset in range(count):
                    destination = scalar_value + offset
                    pairs.append(
                        (f"{first + offset:0{len(start)}X}", f"{destination:04X}")
                    )
            elif array_body is not None:
                destinations = _HEX_TOKEN.findall(array_body)
                if len(destinations) != count:
                    continue
                decoded = [
                    _strict_unicode_from_hex(destination)
                    for destination in destinations
                ]
                if any(value is None for value in decoded):
                    continue
                pairs.extend(
                    (f"{first + offset:0{len(start)}X}", destination.upper())
                    for offset, destination in enumerate(destinations)
                )
    return pairs


def _parse_tounicode_entries(data: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return safe bfchar/bfrange pairs and diagnostics for unsafe syntax."""
    pairs: list[tuple[str, str]] = []
    diagnostics: list[str] = []
    for section in _CMAP_SECTION.finditer(data):
        declared = int(section.group("count") or 0)
        body = section.group("body")
        kind = section.group("kind")
        if kind == "bfchar":
            entries = re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", body
            )
            if declared and len(entries) != declared:
                diagnostics.append("bfchar count does not match entries")
            for source, destination in entries:
                if len(source) > 4 or len(source) % 2:
                    diagnostics.append("bfchar source code is invalid")
                    continue
                if _strict_unicode_from_hex(destination) is None:
                    diagnostics.append("bfchar destination is invalid")
                    continue
                pairs.append((source.upper(), destination.upper()))
            if not entries and body.strip():
                diagnostics.append("bfchar block contains no valid entries")
            continue

        entries = list(_BFRANGE_ENTRY.finditer(body))
        if declared and len(entries) != declared:
            diagnostics.append("bfrange count does not match entries")
        expanded = _parse_bfrange_pairs(
            f"beginbfrange {body} endbfrange"
        )
        pairs.extend(expanded)
        expected = 0
        for entry in entries:
            start, end, _scalar, _array_body = entry.groups()
            if _valid_source_range(start, end):
                expected += int(end, 16) - int(start, 16) + 1
        if entries and len(expanded) != expected:
            diagnostics.append("bfrange entries are invalid or ambiguous")
        elif len(expanded) == 0 and body.strip():
            diagnostics.append("bfrange block contains no valid entries")
    if not _CMAP_SECTION.search(data):
        diagnostics.append("ToUnicode CMap has no bfchar or bfrange block")
    return pairs, diagnostics


def _unicode_from_tounicode_dst(dst: str) -> str:
    return bytes.fromhex(dst).decode("utf-16-be", errors="replace")


def _is_unreliable_tounicode_text(text: str) -> bool:
    return any(
        char == "\ufffd"
        or ord(char) < 0x20
        or 0x7F <= ord(char) <= 0x9F
        or 0xE000 <= ord(char) <= 0xF8FF
        for char in text
    )


@dataclass
class CharacterEncodingRepairResult:
    struct_updated: int
    mcids_updated: int
    fonts_updated: int
    actions: list[str]
    fonts_inspected: int = 0
    missing_tounicode_fonts: list[str] = field(default_factory=list)
    invalid_tounicode_fonts: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _font_tounicode_stream(font: pikepdf.Dictionary) -> object | None:
    stream = font.get("/ToUnicode")
    if stream is not None:
        return stream
    descendants = font.get("/DescendantFonts")
    if isinstance(descendants, pikepdf.Array):
        for descendant in descendants:
            if isinstance(descendant, pikepdf.Dictionary):
                stream = _font_tounicode_stream(descendant)
                if stream is not None:
                    return stream
    return None


def _load_tounicode_map(font: pikepdf.Dictionary) -> dict[int, str]:
    stream = _font_tounicode_stream(font)
    if stream is None:
        return {}
    try:
        data = stream.read_bytes().decode("latin1", errors="replace")
    except (AttributeError, pikepdf.PdfError):
        return {}
    pairs, _diagnostics = _parse_tounicode_entries(data)
    return {int(src, 16): _unicode_from_tounicode_dst(dst) for src, dst in pairs}


def _decode_mcid_text(
    body: bytes,
    fontmaps: dict[str, dict[int, str]],
) -> str:
    current: str | None = None
    in_bt = False
    parts: list[str] = []
    for chunk in re.split(rb"(BT|ET)", body):
        if chunk == b"BT":
            in_bt = True
            continue
        if chunk == b"ET":
            in_bt = False
            current = None
            continue
        if not in_bt:
            continue
        font_match = re.search(rb"/([A-Za-z0-9_]+)\s+[\d.]+\s+Tf", chunk)
        if font_match:
            current = "/" + font_match.group(1).decode()
        for match in re.finditer(rb"<([0-9A-Fa-f]+)>Tj", chunk):
            hex_text = match.group(1).decode()
            cmap = fontmaps.get(current or "", {})
            parts.append(_decode_hex_text(hex_text, cmap))
        for match in re.finditer(rb"\(([^)]*)\)Tj", chunk):
            parts.append(match.group(1).decode("latin1", errors="replace"))
        for match in re.finditer(rb"\[(.*?)\]\s*TJ", chunk, re.DOTALL):
            cmap = fontmaps.get(current or "", {})
            for token in re.finditer(
                rb"\(((?:\\.|[^\\()])*)\)|<([0-9A-Fa-f]+)>",
                match.group(1),
            ):
                if token.group(1) is not None:
                    parts.append(token.group(1).decode("latin1", errors="replace"))
                elif token.group(2) is not None:
                    hex_text = token.group(2).decode()
                    parts.append(_decode_hex_text(hex_text, cmap))
    return "".join(parts)


def _decode_hex_text(hex_text: str, cmap: dict[int, str]) -> str:
    if not hex_text or len(hex_text) % 2:
        return "\ufffd"
    widths = [4, 2] if len(hex_text) % 4 == 0 else [2]
    for width in widths:
        codes = [
            int(hex_text[index : index + width], 16)
            for index in range(0, len(hex_text), width)
        ]
        if all(code in cmap for code in codes):
            return "".join(cmap[code] for code in codes)
    width = 4 if len(hex_text) % 4 == 0 else 2
    return "".join(
        cmap.get(int(hex_text[index : index + width], 16), "\ufffd")
        for index in range(0, len(hex_text), width)
    )


_BLANK_GLYPH_SHOW_OPERATOR = re.compile(
    rb"(?P<token>\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>"
    rb"|\[(?:\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>|[^\[\]])*\])\s*(?:Tj|TJ)\b",
    re.DOTALL,
)


def _body_has_blank_square_glyph(body: bytes) -> bool:
    """True only when the 0191 glyph is this block's sole rendered content.

    The same glyph also prefixes ordinary bulleted sentences ("[ ] Recall
    the calculation..."), and when a marked-content block spans a shared
    text object (BT opened by a sibling MCID), that whole sentence can live
    in the same block. A bare substring search can't tell the two apart, so
    require every OTHER show operator in the block to draw nothing but
    whitespace/underscore filler before calling it a lone blank answer line.
    """
    if not re.search(rb"<0191(?!\d)", body):
        return False
    for match in _BLANK_GLYPH_SHOW_OPERATOR.finditer(body):
        token = match.group("token")
        if b"0191" in token:
            continue
        if _shown_bytes(token).strip(b" _"):
            return False
    return True


def _spoken_for_symbol_block(body: bytes, decoded: str) -> str | None:
    if _body_has_blank_square_glyph(body):
        return "blank"
    spoken = _spoken_encoding_text(decoded)
    return spoken if spoken else None


def _spoken_encoding_text(text: str) -> str:
    spoken = text
    for symbol, replacement in _SYMBOL_SPOKEN.items():
        spoken = spoken.replace(symbol, replacement)
    spoken = re.sub(r"\s+", " ", spoken).strip()
    return spoken


def _block_has_real_text(decoded: str) -> bool:
    return any(
        not (char in _SYMBOL_CHARS or char.isspace()) for char in decoded
    )


_FONT_OR_SHOW_PATTERN = re.compile(
    rb"/(?P<font>[A-Za-z0-9_.]+)\s+[\d.]+\s+Tf"
    rb"|(?P<show>(?:\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>"
    rb"|\[(?:\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>|[^\[\]])*\])\s*(?:Tj|TJ)\b)",
    re.DOTALL,
)
_SPAN_WRAP_PREFIX = re.compile(
    rb"/Span\s*<<\s*/ActualText[^>]*>>\s*BDC\s*$", re.DOTALL
)


def _decode_show_operator(segment: bytes, cmap: dict[int, str]) -> str:
    parts: list[str] = []
    for token in re.finditer(
        rb"\(((?:\\.|[^\\()])*)\)|<([0-9A-Fa-f\s]+)>", segment
    ):
        if token.group(1) is not None:
            parts.append(token.group(1).decode("latin1", errors="replace"))
        else:
            hex_text = re.sub(rb"\s", b"", token.group(2)).decode()
            parts.append(_decode_hex_text(hex_text, cmap))
    return "".join(parts)


def _wrap_symbol_show_operators(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    *,
    mcid: int,
    actions: list[str],
) -> int:
    """Wrap symbol-only show operators in nested /Span ActualText.

    Used when a marked-content block mixes a spoken symbol glyph (bullet,
    blank square) with real sentence text: block-level ActualText would
    override the sentence, so only the glyph's own show operator gets the
    spoken replacement.
    """
    contents = page.get("/Contents")
    if contents is None:
        return 0
    data = _read_page_contents(contents)
    block = _get_mcid_block_to_emc(data, mcid)
    if block is None:
        return 0
    start, end, body = block
    fontmaps = _page_fontmaps(page)
    current_font: str | None = None
    replacements: list[tuple[int, int, bytes]] = []
    for match in _FONT_OR_SHOW_PATTERN.finditer(body):
        if match.group("font") is not None:
            current_font = "/" + match.group("font").decode()
            continue
        segment = match.group("show")
        if _SPAN_WRAP_PREFIX.search(body[: match.start()]):
            continue
        decoded = _decode_show_operator(
            segment, fontmaps.get(current_font or "", {})
        )
        stripped = decoded.strip()
        if not stripped:
            continue
        if not all(char in _SYMBOL_CHARS or char.isspace() for char in decoded):
            continue
        spoken = _spoken_encoding_text(decoded)
        if not spoken:
            continue
        if decoded[:1].isspace():
            spoken = " " + spoken
        if decoded[-1:].isspace():
            spoken = spoken + " "
        wrapped = (
            b"/Span << /ActualText "
            + _pdf_literal_string(spoken)
            + b" >> BDC "
            + segment
            + b" EMC"
        )
        replacements.append((match.start("show"), match.end("show"), wrapped))
    if not replacements:
        return 0
    for rep_start, rep_end, wrapped in reversed(replacements):
        body = body[:rep_start] + wrapped + body[rep_end:]
    page["/Contents"] = pdf.make_stream(
        data[:start] + body + data[end:], compress=True
    )
    for _rep_start, _rep_end, _wrapped in replacements:
        actions.append(f"MCID {mcid}: wrapped symbol show operator in /Span ActualText")
    return len(replacements)


def _needs_character_encoding_repair(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if any(char in _SYMBOL_CHARS for char in stripped):
        return True
    if ":" in stripped and len(stripped) <= 40 and re.search(r":[A-Za-z]:", stripped):
        return True
    return False


def _effective_page_resources(page: pikepdf.Page) -> pikepdf.Dictionary:
    resources = page.get("/Resources")
    if isinstance(resources, pikepdf.Dictionary):
        return resources
    parent = page.get("/Parent")
    while isinstance(parent, pikepdf.Dictionary):
        resources = parent.get("/Resources")
        if isinstance(resources, pikepdf.Dictionary):
            return resources
        parent = parent.get("/Parent")
    return pikepdf.Dictionary()


def _effective_page_fonts(page: pikepdf.Page) -> list[tuple[str, pikepdf.Dictionary]]:
    fonts = _effective_page_resources(page).get("/Font")
    if not isinstance(fonts, pikepdf.Dictionary):
        return []
    return [
        (str(name), font)
        for name, font in fonts.items()
        if isinstance(font, pikepdf.Dictionary)
    ]


def _page_fontmaps(page: pikepdf.Page) -> dict[str, dict[int, str]]:
    return {
        name: _load_tounicode_map(font)
        for name, font in _effective_page_fonts(page)
    }


def _font_key(font: pikepdf.Dictionary) -> tuple[int, int] | tuple[str, int]:
    key = font.objgen
    if key != (0, 0):
        return key
    return ("direct", id(font))


def _font_label(name: str, font: pikepdf.Dictionary) -> str:
    base = font.get("/BaseFont")
    return f"{name} ({base if base is not None else 'font'})"


def _inspect_page_fonts(
    pdf: pikepdf.Pdf,
) -> tuple[int, list[str], list[str], list[str], dict[tuple[int, int] | tuple[str, int], pikepdf.Dictionary]]:
    inspected = 0
    missing: list[str] = []
    invalid: list[str] = []
    diagnostics: list[str] = []
    fonts_by_key: dict[
        tuple[int, int] | tuple[str, int], pikepdf.Dictionary
    ] = {}
    labels_by_key: dict[tuple[int, int] | tuple[str, int], str] = {}
    for page in pdf.pages:
        for name, font in _effective_page_fonts(page):
            key = _font_key(font)
            fonts_by_key[key] = font
            labels_by_key[key] = _font_label(name, font)
    for key, font in fonts_by_key.items():
        inspected += 1
        label = labels_by_key[key]
        stream = _font_tounicode_stream(font)
        if stream is None:
            missing.append(label)
            diagnostics.append(f"{label}: missing /ToUnicode map")
            continue
        try:
            data = stream.read_bytes().decode("latin1", errors="replace")
        except (AttributeError, pikepdf.PdfError) as error:
            invalid.append(label)
            diagnostics.append(f"{label}: unreadable /ToUnicode map ({error})")
            continue
        pairs, map_diagnostics = _parse_tounicode_entries(data)
        if not pairs or map_diagnostics:
            invalid.append(label)
            for detail in map_diagnostics or ["empty /ToUnicode CMap"]:
                diagnostics.append(f"{label}: {detail}")
    return inspected, missing, invalid, diagnostics, fonts_by_key


def _replace_unreliable_font_tounicode(
    pdf: pikepdf.Pdf,
    font: pikepdf.Dictionary,
) -> bool:
    """Remap only unreliable /ToUnicode destinations to placeholder characters.

    Duplicate destinations are valid PDF (several glyph variants may map to
    the same character) and must be preserved: remapping them destroys real
    extracted text.
    """
    stream = font.get("/ToUnicode")
    if stream is None:
        return False

    data = stream.read_bytes().decode("latin1", errors="replace")
    pairs, _diagnostics = _parse_tounicode_entries(data)
    if not pairs:
        return False

    used_dst: set[str] = set()
    replacements: list[tuple[int, str, str, str]] = []
    next_fallback = 0x2500
    bfchar_pairs = _parse_bfchar_pairs(data)
    for _index, (src, dst) in enumerate(bfchar_pairs):
        if len(src) > 4:
            continue
        used_dst.add(_unicode_from_tounicode_dst(dst))
    for index, (src, dst) in enumerate(bfchar_pairs):
        if len(src) > 4:
            continue
        unicode_char = _unicode_from_tounicode_dst(dst)
        if _is_unreliable_tounicode_text(unicode_char):
            fallback_codepoint = next_fallback
            while chr(fallback_codepoint) in used_dst:
                fallback_codepoint += 1
                if fallback_codepoint > 0x257F:
                    fallback_codepoint = 0x2500
            next_fallback = fallback_codepoint + 1
            if next_fallback > 0x257F:
                next_fallback = 0x2500
            fallback_char = chr(fallback_codepoint)
            used_dst.add(fallback_char)
            fallback_hex = fallback_char.encode("utf-16-be").hex().upper()
            replacements.append((index, src, dst, fallback_hex))

    if not replacements:
        return False

    replacement_map = {
        index: pua_hex for index, _src, _dst, pua_hex in replacements
    }
    bfchar_index = 0

    def replace_bfchar_section(match: re.Match[str]) -> str:
        nonlocal bfchar_index
        section = match.group(1)
        lines: list[str] = []
        for src, dst in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", section
        ):
            new_dst = replacement_map.get(bfchar_index, dst)
            bfchar_index += 1
            lines.append(f"<{src.upper()}> <{new_dst.upper()}>")
        return f"{len(lines)} beginbfchar\r\n" + "\r\n".join(lines) + "\r\nendbfchar"

    new_data = _BFCHAR_SECTION.sub(replace_bfchar_section, data)

    font["/ToUnicode"] = pdf.make_stream(new_data.encode("latin1"))
    return True


def count_ambiguous_tounicode_fonts(pdf_bytes: bytes) -> int:
    """Count duplicate destinations across safe bfchar and bfrange mappings."""
    duplicates = 0
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        seen: set[tuple[int, int] | tuple[str, int]] = set()
        for page in pdf.pages:
            for _name, font in _effective_page_fonts(page):
                key = _font_key(font)
                if key in seen:
                    continue
                seen.add(key)
                stream = _font_tounicode_stream(font)
                if stream is None:
                    continue
                try:
                    data = stream.read_bytes().decode("latin1", errors="replace")
                except (AttributeError, pikepdf.PdfError):
                    continue
                pairs, _diagnostics = _parse_tounicode_entries(data)
                seen_unicode: dict[str, str] = {}
                for src, dst in pairs:
                    unicode_char = _unicode_from_tounicode_dst(dst)
                    if unicode_char in seen_unicode:
                        duplicates += 1
                    else:
                        seen_unicode[unicode_char] = src
    return duplicates


def _font_file_stream(font: pikepdf.Dictionary) -> pikepdf.Object | None:
    descriptor = font.get("/FontDescriptor")
    descendants = font.get("/DescendantFonts")
    if descriptor is None and isinstance(descendants, pikepdf.Array):
        first = descendants[0] if len(descendants) else None
        if isinstance(first, pikepdf.Dictionary):
            descriptor = first.get("/FontDescriptor")
    if not isinstance(descriptor, pikepdf.Dictionary):
        return None
    for key in ("/FontFile2", "/FontFile3", "/FontFile"):
        stream = descriptor.get(key)
        if stream is not None:
            return stream
    return None


def _font_is_identity_cid(font: pikepdf.Dictionary) -> bool:
    if font.get("/Subtype") != "/Type0":
        return False
    if not str(font.get("/Encoding", "")).endswith("Identity-H"):
        return False
    descendants = font.get("/DescendantFonts")
    if not isinstance(descendants, pikepdf.Array) or not len(descendants):
        return False
    first = descendants[0]
    if not isinstance(first, pikepdf.Dictionary):
        return False
    cid_to_gid = first.get("/CIDToGIDMap")
    return cid_to_gid is None or cid_to_gid == "/Identity"


def _truetype_glyph_has_no_outline(font_buffer: bytes, gid: int) -> bool | None:
    """Return True when a TrueType glyph has an empty outline, None if unknown."""
    try:
        if len(font_buffer) < 12:
            return None
        num_tables = int.from_bytes(font_buffer[4:6], "big")
        tables: dict[bytes, tuple[int, int]] = {}
        for index in range(num_tables):
            record = font_buffer[12 + index * 16 : 28 + index * 16]
            if len(record) < 16:
                return None
            tag = record[0:4]
            offset = int.from_bytes(record[8:12], "big")
            length = int.from_bytes(record[12:16], "big")
            tables[tag] = (offset, length)
        if b"head" not in tables or b"loca" not in tables or b"maxp" not in tables:
            return None
        head_offset = tables[b"head"][0]
        loca_format = int.from_bytes(
            font_buffer[head_offset + 50 : head_offset + 52], "big"
        )
        maxp_offset = tables[b"maxp"][0]
        num_glyphs = int.from_bytes(
            font_buffer[maxp_offset + 4 : maxp_offset + 6], "big"
        )
        if gid >= num_glyphs:
            return None
        loca_offset, loca_length = tables[b"loca"]
        if loca_format == 0:
            entry_size = 2
            scale = 2
        else:
            entry_size = 4
            scale = 1
        start_at = loca_offset + gid * entry_size
        end_at = loca_offset + (gid + 1) * entry_size
        if end_at + entry_size > loca_offset + loca_length + entry_size:
            return None
        start = int.from_bytes(font_buffer[start_at : start_at + entry_size], "big")
        end = int.from_bytes(font_buffer[end_at : end_at + entry_size], "big")
        return (end - start) * scale == 0
    except (IndexError, ValueError):
        return None


_STRING_TOKEN = rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>"
_CONTENT_SCANNER = re.compile(
    rb"/(?P<font>[A-Za-z0-9_.]+)\s+[\d.]+\s+Tf"
    rb"|(?P<show>" + _STRING_TOKEN + rb")\s*(?:Tj|')"
    rb"|(?P<arr>\[(?:" + _STRING_TOKEN + rb"|[^\[\]()<>])*\])\s*TJ"
    rb"|(?P<skipstr>" + _STRING_TOKEN + rb")"
    rb"|(?P<push>\bq\b)"
    rb"|(?P<pop>\bQ\b)",
    re.DOTALL,
)
_STRING_PATTERN = re.compile(
    rb"<([0-9A-Fa-f\s]*)>|\(((?:\\.|[^\\()])*)\)",
    re.DOTALL,
)


def _decode_literal_bytes(literal: bytes) -> bytes:
    out = bytearray()
    index = 0
    while index < len(literal):
        char = literal[index : index + 1]
        if char != b"\\":
            out += char
            index += 1
            continue
        escape = literal[index + 1 : index + 2]
        if escape in (b"n", b"r", b"t", b"b", b"f"):
            out += {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}[
                escape
            ]
            index += 2
        elif escape.isdigit():
            digits = literal[index + 1 : index + 4]
            octal = b""
            for digit in digits:
                if chr(digit).isdigit():
                    octal += bytes([digit])
                else:
                    break
            out.append(int(octal, 8) & 0xFF)
            index += 1 + len(octal)
        else:
            out += escape
            index += 2
    return bytes(out)


def _shown_bytes(token: bytes) -> bytes:
    raw = b""
    for match in _STRING_PATTERN.finditer(token):
        if match.group(1) is not None:
            cleaned = re.sub(rb"\s+", rb"", match.group(1))
            if len(cleaned) % 2:
                cleaned += b"0"
            raw += bytes.fromhex(cleaned.decode("ascii"))
        else:
            raw += _decode_literal_bytes(match.group(2))
    return raw


def _used_codes_for_font(
    data: bytes,
    resource_names: set[str],
    *,
    two_byte: bool,
) -> set[int]:
    """Collect character codes shown with the named fonts, tracking q/Q state."""
    codes: set[int] = set()
    current: str | None = None
    stack: list[str | None] = []
    for match in _CONTENT_SCANNER.finditer(data):
        if match.group("font") is not None:
            current = "/" + match.group("font").decode("latin1", errors="replace")
            continue
        if match.group("push") is not None:
            stack.append(current)
            continue
        if match.group("pop") is not None:
            if stack:
                current = stack.pop()
            continue
        if match.group("skipstr") is not None:
            continue
        if current not in resource_names:
            continue
        token = match.group("show")
        if token is None:
            token = match.group("arr")
        raw = _shown_bytes(token)
        if two_byte:
            for offset in range(0, len(raw) - 1, 2):
                codes.add(int.from_bytes(raw[offset : offset + 2], "big"))
        else:
            codes.update(raw)
    return codes


def _build_tounicode_cmap(entries: dict[int, str], *, two_byte: bool) -> bytes:
    width = 4 if two_byte else 2
    space_end = "FFFF" if two_byte else "FF"
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        f"<{'0' * width}> <{space_end}>",
        "endcodespacerange",
        f"{len(entries)} beginbfchar",
    ]
    for code in sorted(entries):
        destination = entries[code].encode("utf-16-be").hex().upper()
        lines.append(f"<{code:0{width}X}> <{destination}>")
    lines.extend(["endbfchar", "endcmap", "end", "end"])
    return "\r\n".join(lines).encode("latin1")


def _winansi_char(code: int) -> str | None:
    """The character a /WinAnsiEncoding code denotes, or None if undefined."""
    if code < 0x20:
        return None
    try:
        return bytes([code]).decode("cp1252")
    except UnicodeDecodeError:
        return None


def _font_code_space_signature(font: pikepdf.Dictionary) -> str:
    """Identify the code space a font's ToUnicode map is keyed by.

    Maps are only interchangeable between fonts whose codes mean the same
    thing: a Type0 Identity map is CID/GID-keyed while a simple font's map is
    byte-code-keyed, so sharing an embedded font program is not enough.
    """
    if font.get("/Subtype") == "/Type0":
        return "cid-identity" if _font_is_identity_cid(font) else "cid-other"
    encoding = font.get("/Encoding")
    if encoding is None:
        return "simple:none"
    if isinstance(encoding, pikepdf.Dictionary):
        base = encoding.get("/BaseEncoding")
        diffs = encoding.get("/Differences")
        return f"simple:{base}:diffs={'y' if diffs is not None else 'n'}"
    return f"simple:{encoding}"


def _repair_missing_tounicode(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Give ToUnicode maps to fonts that lack one.

    A sibling font instance sharing the same embedded font program donates its
    map only when both fonts key codes the same way (identical code space).
    Codes still unmapped are resolved from evidence: a /WinAnsiEncoding code
    means what the WinAnsi table says, a used TrueType glyph with an empty
    outline is whitespace, a simple-font code that names a valid codepoint in
    the font program maps to itself, and anything else gets a reliable
    placeholder character.
    """
    fonts_by_key: dict[tuple[int, int] | tuple[str, int], pikepdf.Dictionary] = {}
    names_by_key: dict[tuple[int, int] | tuple[str, int], set[str]] = {}
    pages_by_key: dict[tuple[int, int] | tuple[str, int], list[pikepdf.Page]] = {}
    for page in pdf.pages:
        for name, font in _effective_page_fonts(page):
            key = _font_key(font)
            fonts_by_key[key] = font
            names_by_key.setdefault(key, set()).add(name)
            pages_by_key.setdefault(key, []).append(page)

    donors: dict[tuple[tuple[int, int], str], pikepdf.Object] = {}
    for font in fonts_by_key.values():
        stream = _font_tounicode_stream(font)
        file_stream = _font_file_stream(font)
        if stream is not None and file_stream is not None:
            donors.setdefault(
                (file_stream.objgen, _font_code_space_signature(font)),
                stream,
            )

    updated = 0
    for key, font in fonts_by_key.items():
        if _font_tounicode_stream(font) is not None:
            continue
        is_cid = font.get("/Subtype") == "/Type0"
        if is_cid and not _font_is_identity_cid(font):
            continue
        file_stream = _font_file_stream(font)
        donor = (
            donors.get(
                (file_stream.objgen, _font_code_space_signature(font))
            )
            if file_stream is not None
            else None
        )

        entries: dict[int, str] = {}
        donor_data = ""
        if donor is not None:
            donor_data = donor.read_bytes().decode("latin1", errors="replace")
            pairs, _diagnostics = _parse_tounicode_entries(donor_data)
            for src, dst in pairs:
                entries.setdefault(int(src, 16), _unicode_from_tounicode_dst(dst))

        used: set[int] = set()
        for page in pages_by_key.get(key, []):
            data = _read_page_contents(page.get("/Contents"))
            if data:
                used.update(
                    _used_codes_for_font(data, names_by_key[key], two_byte=is_cid)
                )

        font_buffer = (
            file_stream.read_bytes() if file_stream is not None else b""
        )
        valid_codepoints: set[int] = set()
        if font_buffer and not is_cid:
            try:
                probe = pymupdf.Font(fontbuffer=font_buffer)
                valid_codepoints = set(probe.valid_codepoints())
            except (RuntimeError, ValueError):
                valid_codepoints = set()

        is_winansi = not is_cid and font.get("/Encoding") == pikepdf.Name(
            "/WinAnsiEncoding"
        )
        used_dst = set(entries.values())
        next_fallback = 0x2500
        added: dict[int, str] = {}
        for code in sorted(used - set(entries)):
            winansi_char = _winansi_char(code) if is_winansi else None
            if is_cid and font_buffer and _truetype_glyph_has_no_outline(
                font_buffer, code
            ):
                added[code] = " "
            elif winansi_char is not None:
                added[code] = winansi_char
            elif not is_cid and 0x20 <= code <= 0x7E and code in valid_codepoints:
                added[code] = chr(code)
            else:
                fallback = next_fallback
                while chr(fallback) in used_dst:
                    fallback += 1
                    if fallback > 0x257F:
                        fallback = 0x2500
                next_fallback = fallback + 1
                if next_fallback > 0x257F:
                    next_fallback = 0x2500
                added[code] = chr(fallback)
                used_dst.add(chr(fallback))

        if not entries and not added:
            continue

        entries.update(added)
        font["/ToUnicode"] = pdf.make_stream(
            _build_tounicode_cmap(entries, two_byte=is_cid)
        )
        updated += 1
        label = str(font.get("/BaseFont", "font"))
        detail = "borrowed sibling map" if donor is not None else "synthesized map"
        if added:
            detail += f", derived {len(added)} used codes"
        actions.append(f"added missing /ToUnicode to {label} ({detail})")
    return updated


def _repair_font_tounicode_reliability(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    updated = 0
    seen: set[tuple[int, int] | tuple[str, int]] = set()
    for page in pdf.pages:
        for _name, font in _effective_page_fonts(page):
            key = _font_key(font)
            if key in seen:
                continue
            seen.add(key)
            if _replace_unreliable_font_tounicode(pdf, font):
                updated += 1
                actions.append(
                    "replaced unreliable /ToUnicode destinations in "
                    f"{font.get('/BaseFont', 'font')}"
                )
    return updated


def _needs_orphan_symbol_repair(body: bytes, decoded: str) -> bool:
    if _body_has_blank_square_glyph(body):
        return True
    stripped = decoded.strip()
    return any(char in "□●αβ" for char in stripped)


def _repair_orphan_symbol_mcids(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    """Inject /ActualText on symbol MCIDs not reachable from the struct tree walk."""
    updated = 0
    for page in pdf.pages:
        contents = page.get("/Contents")
        if contents is None:
            continue
        data = _read_page_contents(contents)
        if not data:
            continue
        fontmaps = _page_fontmaps(page)
        mcids = sorted(
            {int(match.group(1)) for match in _CONTENT_MCID_PATTERN.finditer(data)}
        )
        for mcid in mcids:
            block = _get_mcid_block_to_emc(data, mcid)
            if block is None:
                continue
            if _mcid_bdc_has_actualtext(data, mcid):
                continue
            _start, _end, body = block
            decoded = _decode_mcid_text(body, fontmaps)
            if not _needs_orphan_symbol_repair(body, decoded):
                continue
            if _block_has_real_text(decoded) and not _body_has_blank_square_glyph(
                body
            ):
                updated += _wrap_symbol_show_operators(
                    pdf, page, mcid=mcid, actions=actions
                )
                continue
            spoken = _spoken_for_symbol_block(body, decoded)
            if not spoken:
                continue
            if _inject_actualtext_on_page(
                pdf,
                page,
                mcid=mcid,
                actual_text=spoken,
                replace_existing=False,
            ):
                updated += 1
                actions.append(
                    f"orphan MCID {mcid}: injected /ActualText for {spoken!r}"
                )
    return updated


def repair_character_encoding(pdf_bytes: bytes) -> tuple[bytes, CharacterEncodingRepairResult]:
    struct_updated = 0
    mcids_updated = 0
    actions: list[str] = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        fonts_updated = _repair_missing_tounicode(pdf, actions=actions)
        (
            fonts_inspected,
            missing_tounicode_fonts,
            invalid_tounicode_fonts,
            diagnostics,
            fonts_by_key,
        ) = _inspect_page_fonts(pdf)

        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is not None:

            def walk(obj: pikepdf.Dictionary) -> None:
                nonlocal struct_updated, mcids_updated
                tag = obj.get("/S")
                if tag not in _STRUCT_TAGS:
                    for child in _struct_child_dicts(obj):
                        walk(child)
                    return

                page = _resolve_struct_page(pdf, obj)
                if page is None:
                    for child in _struct_child_dicts(obj):
                        walk(child)
                    return

                mcids = _struct_element_mcids(obj)
                if not mcids:
                    for child in _struct_child_dicts(obj):
                        walk(child)
                    return

                data = _read_page_contents(page.get("/Contents"))
                if not data:
                    for child in _struct_child_dicts(obj):
                        walk(child)
                    return

                fontmaps = _page_fontmaps(page)
                decoded_parts: list[str] = []
                for mcid in mcids:
                    block = _get_mcid_block_to_emc(data, mcid)
                    if block is None:
                        continue
                    decoded_parts.append(_decode_mcid_text(block[2], fontmaps))
                decoded = "".join(decoded_parts)
                if not _needs_character_encoding_repair(decoded):
                    for child in _struct_child_dicts(obj):
                        walk(child)
                    return

                spoken = _spoken_encoding_text(decoded)
                if not spoken:
                    for child in _struct_child_dicts(obj):
                        walk(child)
                    return

                element_mixed = False
                for mcid in mcids:
                    block = _get_mcid_block_to_emc(data, mcid)
                    if block is None:
                        continue
                    part = _decode_mcid_text(block[2], fontmaps)
                    if (
                        any(char in _SYMBOL_CHARS for char in part)
                        and _block_has_real_text(part)
                        and not _body_has_blank_square_glyph(block[2])
                    ):
                        element_mixed = True
                        break

                if not element_mixed and obj.get("/ActualText") is None:
                    obj["/ActualText"] = pikepdf.String(spoken)
                    struct_updated += 1
                    actions.append(f"set struct /ActualText on {tag} for {spoken!r}")

                for mcid in mcids:
                    block = _get_mcid_block_to_emc(data, mcid)
                    if block is None:
                        continue
                    part = _decode_mcid_text(block[2], fontmaps)
                    if (
                        any(char in _SYMBOL_CHARS for char in part)
                        and _block_has_real_text(part)
                        and not _body_has_blank_square_glyph(block[2])
                    ):
                        mcids_updated += _wrap_symbol_show_operators(
                            pdf, page, mcid=mcid, actions=actions
                        )
                        continue
                    part_spoken = _spoken_for_symbol_block(block[2], part)
                    if part_spoken is None and not _needs_character_encoding_repair(part):
                        continue
                    if part_spoken is None:
                        part_spoken = _spoken_encoding_text(part)
                    if not part_spoken:
                        continue
                    if _inject_actualtext_on_page(
                        pdf,
                        page,
                        mcid=mcid,
                        actual_text=part_spoken,
                        replace_existing=False,
                    ):
                        mcids_updated += 1
                        actions.append(
                            f"injected /ActualText on MCID {mcid} for {part_spoken!r}"
                        )

                for child in _struct_child_dicts(obj):
                    walk(child)

            walk(struct_root)

        mcids_updated += _repair_orphan_symbol_mcids(pdf, actions=actions)
        fonts_updated += _repair_font_tounicode_reliability(pdf, actions=actions)

        result = CharacterEncodingRepairResult(
            struct_updated=struct_updated,
            mcids_updated=mcids_updated,
            fonts_updated=fonts_updated,
            actions=actions,
            fonts_inspected=fonts_inspected,
            missing_tounicode_fonts=missing_tounicode_fonts,
            invalid_tounicode_fonts=invalid_tounicode_fonts,
            diagnostics=diagnostics,
        )
        if not actions:
            return pdf_bytes, result
        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue(), result
