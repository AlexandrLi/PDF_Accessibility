"""Repair Acrobat character-encoding failures for symbol glyphs and ambiguous CMaps."""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field

import pikepdf

from lib.marked_content_actualtext_sweep import (
    _get_mcid_block,
    _inject_actualtext_on_page,
    _mcid_bdc_has_actualtext,
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


def _body_has_blank_square_glyph(body: bytes) -> bool:
    return bool(re.search(rb"<0191", body))


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


def _font_is_established_composite_case(font: pikepdf.Dictionary) -> bool:
    """Keep the legacy repair for generated Identity-H composite fonts."""
    return (
        font.get("/Subtype") == "/Type0"
        and str(font.get("/Encoding", "")).endswith("Identity-H")
    )


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


def _dedupe_font_tounicode(pdf: pikepdf.Pdf, font: pikepdf.Dictionary) -> bool:
    stream = font.get("/ToUnicode")
    if stream is None:
        return False

    data = stream.read_bytes().decode("latin1", errors="replace")
    pairs, _diagnostics = _parse_tounicode_entries(data)
    if not pairs:
        return False

    seen_unicode: dict[str, str] = {}
    used_dst: set[str] = set()
    replacements: list[tuple[int, str, str, str]] = []
    next_pua = 0xF000
    bfchar_pairs = _parse_bfchar_pairs(data)
    for index, (src, dst) in enumerate(bfchar_pairs):
        if len(src) > 4:
            continue
        unicode_char = _unicode_from_tounicode_dst(dst)
        used_dst.add(unicode_char)
        if unicode_char in seen_unicode:
            pua_codepoint = next_pua
            while chr(pua_codepoint) in used_dst:
                pua_codepoint += 1
                if pua_codepoint > 0xF8FF:
                    pua_codepoint = 0xF000
            next_pua = pua_codepoint + 1
            if next_pua > 0xF8FF:
                next_pua = 0xF000
            pua_char = chr(pua_codepoint)
            used_dst.add(pua_char)
            pua_hex = pua_char.encode("utf-16-be").hex().upper()
            replacements.append((index, src, dst, pua_hex))
        else:
            seen_unicode[unicode_char] = src

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


def _repair_font_tounicode_ambiguity(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
    eligible_keys: set[tuple[int, int] | tuple[str, int]] | None = None,
) -> int:
    updated = 0
    seen: set[tuple[int, int] | tuple[str, int]] = set()
    for page in pdf.pages:
        for _name, font in _effective_page_fonts(page):
            key = _font_key(font)
            if key in seen:
                continue
            seen.add(key)
            if eligible_keys is not None and key not in eligible_keys:
                continue
            if _dedupe_font_tounicode(pdf, font):
                updated += 1
                actions.append(
                    f"deduped ambiguous /ToUnicode mappings in {font.get('/BaseFont', 'font')}"
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
            block = _get_mcid_block(data, mcid)
            if block is None:
                continue
            if _mcid_bdc_has_actualtext(data, mcid):
                continue
            _tag, body = block
            decoded = _decode_mcid_text(body, fontmaps)
            if not _needs_orphan_symbol_repair(body, decoded):
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


def _font_keys_with_actualtext_fallback(
    pdf: pikepdf.Pdf,
) -> set[tuple[int, int] | tuple[str, int]]:
    """Find fonts whose ambiguous extraction is protected by /ActualText."""
    protected: set[tuple[int, int] | tuple[str, int]] = set()
    page_fonts = {
        page.objgen: {name: _font_key(font) for name, font in _effective_page_fonts(page)}
        for page in pdf.pages
    }

    def collect_from_body(page: pikepdf.Page, body: bytes) -> None:
        fonts = page_fonts.get(page.objgen, {})
        for match in re.finditer(rb"/([A-Za-z0-9_]+)\s+[\d.]+\s+Tf", body):
            key = fonts.get("/" + match.group(1).decode("ascii"))
            if key is not None:
                protected.add(key)

    for page in pdf.pages:
        data = _read_page_contents(page.get("/Contents"))
        if not data:
            continue
        mcids = sorted(
            {int(match.group(1)) for match in _CONTENT_MCID_PATTERN.finditer(data)}
        )
        for mcid in mcids:
            block = _get_mcid_block(data, mcid)
            if block is None:
                continue
            if _mcid_bdc_has_actualtext(data, mcid):
                collect_from_body(page, block[1])

    struct_root = pdf.Root.get("/StructTreeRoot")
    if struct_root is None:
        return protected

    def walk(obj: pikepdf.Dictionary) -> None:
        if obj.get("/ActualText") is not None:
            page = _resolve_struct_page(pdf, obj)
            if page is not None:
                contents = page.get("/Contents")
                if contents is None:
                    data = b""
                else:
                    data = _read_page_contents(contents)
                for mcid in _struct_element_mcids(obj):
                    block = _get_mcid_block(data, mcid)
                    if block is not None:
                        collect_from_body(page, block[1])
        for child in _struct_child_dicts(obj):
            walk(child)

    walk(struct_root)
    return protected


def repair_character_encoding(pdf_bytes: bytes) -> tuple[bytes, CharacterEncodingRepairResult]:
    struct_updated = 0
    mcids_updated = 0
    actions: list[str] = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        (
            fonts_inspected,
            missing_tounicode_fonts,
            invalid_tounicode_fonts,
            diagnostics,
            fonts_by_key,
        ) = _inspect_page_fonts(pdf)
        fonts_updated = 0

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
                    block = _get_mcid_block(data, mcid)
                    if block is None:
                        continue
                    decoded_parts.append(_decode_mcid_text(block[1], fontmaps))
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

                if obj.get("/ActualText") is None:
                    obj["/ActualText"] = pikepdf.String(spoken)
                    struct_updated += 1
                    actions.append(f"set struct /ActualText on {tag} for {spoken!r}")

                for mcid in mcids:
                    block = _get_mcid_block(data, mcid)
                    if block is None:
                        continue
                    part = _decode_mcid_text(block[1], fontmaps)
                    part_spoken = _spoken_for_symbol_block(block[1], part)
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
        eligible_font_keys = _font_keys_with_actualtext_fallback(pdf)
        eligible_font_keys.update(
            key
            for key, font in fonts_by_key.items()
            if _font_is_established_composite_case(font)
        )
        fonts_updated = _repair_font_tounicode_ambiguity(
            pdf,
            actions=actions,
            eligible_keys=eligible_font_keys,
        )

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
