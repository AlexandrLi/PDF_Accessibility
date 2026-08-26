"""Conservatively normalize standard PDF heading nesting."""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass, field

import pikepdf


_HEADING_LEVELS = {f"/H{level}": level for level in range(1, 7)}


@dataclass
class HeadingNestingRepairResult:
    """Facts and nonblocking diagnostics produced by the heading sweep."""

    pages_found: int
    struct_elements_found: int
    headings_found: int
    headings_updated: int
    changed_roles: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def standard_headings_found(self) -> int:
        """Compatibility alias describing numbered headings inspected."""
        return self.headings_found

    @property
    def headings_repaired(self) -> int:
        """Compatibility alias for the number of changed heading roles."""
        return self.headings_updated


def _object_key(obj: object) -> tuple[int, int] | int:
    objgen = getattr(obj, "objgen", None)
    if isinstance(objgen, tuple) and len(objgen) == 2 and objgen != (0, 0):
        return objgen
    return id(obj)


def _is_struct_elem(obj: object) -> bool:
    return isinstance(obj, pikepdf.Dictionary) and (
        str(obj.get("/Type")) == "/StructElem" or obj.get("/S") is not None
    )


def _struct_children(value: object) -> list[pikepdf.Dictionary]:
    """Return structure-element descendants directly contained by ``/K``."""
    if isinstance(value, pikepdf.Dictionary):
        if _is_struct_elem(value):
            return [value]
        nested = value.get("/K")
        return _struct_children(nested) if nested is not None else []
    if not isinstance(value, pikepdf.Array):
        return []

    children: list[pikepdf.Dictionary] = []
    for item in value:
        if isinstance(item, pikepdf.Dictionary):
            if _is_struct_elem(item):
                children.append(item)
            else:
                children.extend(_struct_children(item))
        elif isinstance(item, pikepdf.Array):
            children.extend(_struct_children(item))
    return children


def _role_map(
    root: pikepdf.Dictionary,
) -> tuple[pikepdf.Dictionary | None, str | None]:
    value = root.get("/RoleMap")
    if value is None:
        return None, None
    if not isinstance(value, pikepdf.Dictionary):
        return None, "document /RoleMap is not a dictionary"
    return value, None


def _mapped_role(
    role: str,
    role_map: pikepdf.Dictionary | None,
    role_map_issue: str | None,
) -> tuple[str | None, str | None]:
    """Resolve a custom role enough to report, never to normalize, it."""
    if role in _HEADING_LEVELS or role == "/H":
        return role, None
    if role_map is None:
        return None, role_map_issue or "no document /RoleMap"

    current = role
    seen: set[str] = set()
    while current not in _HEADING_LEVELS and current != "/H":
        if current in seen:
            return None, "cyclic /RoleMap"
        seen.add(current)
        mapped = role_map.get(current)
        if mapped is None:
            return None, "no /RoleMap entry"
        mapped_role = str(mapped)
        if not mapped_role.startswith("/"):
            return None, f"invalid /RoleMap target {mapped_role!r}"
        current = mapped_role
    return current, None


def _collect_structure_elements(
    root: pikepdf.Dictionary,
) -> tuple[list[pikepdf.Dictionary], list[str]]:
    elements: list[pikepdf.Dictionary] = []
    diagnostics: list[str] = []
    seen: set[tuple[int, int] | int] = set()

    def walk(element: pikepdf.Dictionary) -> None:
        key = _object_key(element)
        if key in seen:
            diagnostics.append(
                f"structure element {len(elements) + 1}: repeated reference skipped"
            )
            return
        seen.add(key)
        elements.append(element)
        for child in _struct_children(element.get("/K")):
            walk(child)

    for child in _struct_children(root.get("/K")):
        walk(child)
    return elements, diagnostics


def repair_heading_nesting(
    pdf_bytes: bytes,
) -> tuple[bytes, HeadingNestingRepairResult]:
    """Lower only standard numbered heading roles that violate nesting.

    The structure tree's existing preorder is treated as document order.  Custom
    roles, generic ``/H``, content, and all page objects are preserved.
    """
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        root = pdf.Root.get("/StructTreeRoot")
        if not isinstance(root, pikepdf.Dictionary):
            result = HeadingNestingRepairResult(
                pages_found=len(pdf.pages),
                struct_elements_found=0,
                headings_found=0,
                headings_updated=0,
                unresolved=["document has no /StructTreeRoot"],
                diagnostics=["document has no /StructTreeRoot; no heading roles inspected"],
            )
            return pdf_bytes, result

        elements, diagnostics = _collect_structure_elements(root)
        role_map, role_map_issue = _role_map(root)
        unresolved: list[str] = []
        standard_headings: list[tuple[int, pikepdf.Dictionary, str]] = []
        if role_map_issue is not None:
            unresolved.append(role_map_issue)
            diagnostics.append(role_map_issue)

        for index, element in enumerate(elements, start=1):
            role = str(element.get("/S")) if element.get("/S") is not None else None
            if role is None:
                unresolved.append(f"structure element {index}: missing /S role")
                continue
            if role in _HEADING_LEVELS:
                standard_headings.append((index, element, role))
                continue

            if role == "/H":
                diagnostics.append(
                    f"structure element {index}: generic /H preserved; role not normalized"
                )
                continue
            if role_map is None or role_map.get(role) is None:
                continue

            mapped, mapping_issue = _mapped_role(role, role_map, role_map_issue)
            if mapped in _HEADING_LEVELS or mapped == "/H" or mapping_issue:
                detail = (
                    f"maps to {mapped}"
                    if mapped is not None
                    else mapping_issue or "uncertain role mapping"
                )
                diagnostic = (
                    f"structure element {index}: custom role {role} preserved "
                    f"({detail}; role mapping is not guessed)"
                )
                diagnostics.append(diagnostic)
                unresolved.append(diagnostic)

        previous_level: int | None = None
        changed_roles: list[str] = []
        actions: list[str] = []
        headings_updated = 0
        for index, element, original_role in standard_headings:
            original_level = _HEADING_LEVELS[original_role]
            normalized_level = (
                1
                if previous_level is None
                else min(original_level, previous_level + 1)
            )
            previous_level = normalized_level
            if normalized_level == original_level:
                continue

            normalized_role = f"/H{normalized_level}"
            element["/S"] = pikepdf.Name(normalized_role)
            headings_updated += 1
            changed_roles.append(f"element {index}: {original_role} -> {normalized_role}")
            action = (
                f"structure element {index}: changed heading role "
                f"{original_role} to {normalized_role}"
            )
            actions.append(action)
            diagnostics.append(action)

        result = HeadingNestingRepairResult(
            pages_found=len(pdf.pages),
            struct_elements_found=len(elements),
            headings_found=len(standard_headings),
            headings_updated=headings_updated,
            changed_roles=changed_roles,
            unresolved=unresolved,
            diagnostics=diagnostics,
            actions=actions,
        )
        if headings_updated == 0:
            return pdf_bytes, result

        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue(), result
