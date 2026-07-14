"""Repair Acrobat character-encoding failures for symbol glyphs and ambiguous CMaps."""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass

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

_BFCHAR_SECTION = re.compile(r"beginbfchar\s*(.*?)endbfchar", re.DOTALL)


def _parse_bfchar_pairs(data: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for match in _BFCHAR_SECTION.finditer(data):
        section = match.group(1)
        pairs.extend(re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", section))
    return pairs


def _unicode_from_tounicode_dst(dst: str) -> str:
    return bytes.fromhex(dst).decode("utf-16-be", errors="replace")


@dataclass
class CharacterEncodingRepairResult:
    struct_updated: int
    mcids_updated: int
    fonts_updated: int
    actions: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _load_tounicode_map(font: pikepdf.Dictionary) -> dict[int, str]:
    stream = font.get("/ToUnicode")
    if stream is None:
        return {}
    data = stream.read_bytes().decode("latin1", errors="replace")
    mapping: dict[int, str] = {}
    for src, dst in _parse_bfchar_pairs(data):
        if len(src) > 4:
            continue
        mapping[int(src, 16)] = _unicode_from_tounicode_dst(dst)
    return mapping


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
            cids = [int(hex_text[i : i + 4], 16) for i in range(0, len(hex_text), 4)]
            cmap = fontmaps.get(current or "", {})
            parts.append("".join(cmap.get(cid, "\ufffd") for cid in cids))
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
                    cids = [
                        int(hex_text[i : i + 4], 16)
                        for i in range(0, len(hex_text), 4)
                    ]
                    parts.append("".join(cmap.get(cid, "\ufffd") for cid in cids))
    return "".join(parts)


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


def _page_fontmaps(page: pikepdf.Page) -> dict[str, dict[int, str]]:
    fonts = page.get("/Resources", {}).get("/Font", {})
    if not fonts:
        return {}
    return {
        str(name): _load_tounicode_map(font)
        for name, font in fonts.items()
        if "/ToUnicode" in font
    }


def _dedupe_font_tounicode(pdf: pikepdf.Pdf, font: pikepdf.Dictionary) -> bool:
    stream = font.get("/ToUnicode")
    if stream is None:
        return False

    data = stream.read_bytes().decode("latin1", errors="replace")
    pairs = _parse_bfchar_pairs(data)
    if not pairs:
        return False

    seen_unicode: dict[str, str] = {}
    replacements: list[tuple[str, str, str]] = []
    for src, dst in pairs:
        if len(src) > 4:
            continue
        unicode_char = _unicode_from_tounicode_dst(dst)
        if unicode_char in seen_unicode:
            pua = f"U+{0xE000 + int(src, 16):04X}"
            replacements.append((src, dst, pua))
        else:
            seen_unicode[unicode_char] = src

    if not replacements:
        return False

    def replace_bfchar_section(match: re.Match[str]) -> str:
        section = match.group(1)
        new_section = section
        for src, dst, pua in replacements:
            pua_hex = pua.encode("utf-16-be").hex().upper()
            new_section = re.sub(
                rf"<{src}>\s*<{re.escape(dst)}>",
                f"<{src}><{pua_hex}>",
                new_section,
                count=1,
            )
        return f"beginbfchar{new_section}endbfchar"

    new_data = _BFCHAR_SECTION.sub(replace_bfchar_section, data)

    font["/ToUnicode"] = pdf.make_stream(new_data.encode("latin1"))
    return True


def _repair_font_tounicode_ambiguity(
    pdf: pikepdf.Pdf,
    *,
    actions: list[str],
) -> int:
    updated = 0
    seen: set[tuple[int, int]] = set()
    for page in pdf.pages:
        fonts = page.get("/Resources", {}).get("/Font", {})
        if not fonts:
            continue
        for font in fonts.values():
            key = font.objgen
            if key in seen:
                continue
            seen.add(key)
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
        data = _read_page_contents(page.get("/Contents"))
        if not data:
            continue
        fontmaps = _page_fontmaps(page)
        for mcid in range(1, 301):
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


def repair_character_encoding(pdf_bytes: bytes) -> tuple[bytes, CharacterEncodingRepairResult]:
    struct_updated = 0
    mcids_updated = 0
    actions: list[str] = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        fonts_updated = _repair_font_tounicode_ambiguity(pdf, actions=actions)

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

        output = io.BytesIO()
        pdf.save(output)
        result = CharacterEncodingRepairResult(
            struct_updated=struct_updated,
            mcids_updated=mcids_updated,
            fonts_updated=fonts_updated,
            actions=actions,
        )
        return output.getvalue(), result
