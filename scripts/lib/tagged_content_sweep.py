"""Repair objectively derivable PDF tagged-content relationships."""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from numbers import Integral

import pikepdf

from lib.marked_content_actualtext_sweep import (
    _body_has_show_ops,
    _body_paints_no_content,
    _font_in_effect_at,
    _get_mcid_block_to_emc,
    _iter_bdc_mcids_on_page,
    _page_contents_data,
    _page_font_code_maps,
)


@dataclass
class TaggedContentDiagnostics:
    struct_tree_root_present: bool
    parent_tree_present: bool
    pages_with_struct_parents: int
    pages_with_mapped_mcids: int
    mapped_mcid_count: int
    unresolved_mcids: list[str] = field(default_factory=list)
    parent_tree_keys: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TaggedContentRepairResult:
    pages_found: int
    struct_elements_found: int
    pages_updated: int
    struct_elements_updated: int
    parent_tree_entries_added: int
    parent_tree_entries_updated: int
    parent_tree_next_key_updated: bool
    mapped_mcids: int
    unresolved_mcids: list[str]
    unresolved_associations: list[str]
    conflicts: list[str]
    actions: list[str]
    struct_elements_added: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def unresolved(self) -> list[str]:
        return self.unresolved_mcids + self.unresolved_associations

    @property
    def struct_elements_repaired(self) -> int:
        return self.struct_elements_updated

    @property
    def parent_links_repaired(self) -> int:
        return self.struct_elements_updated

    @property
    def parent_tree_entries_repaired(self) -> int:
        return self.parent_tree_entries_added

    @property
    def parent_tree_conflicts(self) -> list[str]:
        return self.conflicts


@dataclass
class _StructureScan:
    elements: list[pikepdf.Dictionary]
    associations: dict[tuple[int, int], list[pikepdf.Dictionary]]
    unresolved_associations: list[str]
    page_mcids: dict[tuple[int, int], set[int]]
    page_numbers: dict[tuple[int, int], int]
    pageless_mcids: set[int] = field(default_factory=set)


_CONTENT_MCID_PATTERN = re.compile(
    rb"/[^\s<>{}\[\]()/%]+\s*<<(?:(?!>>).)*?/MCID\s+(\d+)(?!\d)",
    re.DOTALL,
)


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


def _is_struct_elem(obj: object) -> bool:
    if not isinstance(obj, pikepdf.Dictionary):
        return False
    return str(obj.get("/Type")) == "/StructElem" or obj.get("/S") is not None


def _as_int(value: object) -> int | None:
    return int(value) if isinstance(value, Integral) else None


def _page_map(pdf: pikepdf.Pdf) -> tuple[dict[tuple[int, int] | int, pikepdf.Page], dict[tuple[int, int] | int, int]]:
    pages: dict[tuple[int, int] | int, pikepdf.Page] = {}
    numbers: dict[tuple[int, int] | int, int] = {}
    for page_number, page in enumerate(pdf.pages, start=1):
        key = _object_key(page.obj)
        pages[key] = page
        numbers[key] = page_number
    return pages, numbers


def _page_for_ref(
    reference: object,
    pages: dict[tuple[int, int] | int, pikepdf.Page],
) -> pikepdf.Page | None:
    if reference is None:
        return None
    return pages.get(_object_key(reference))


def _struct_children(obj: pikepdf.Dictionary) -> list[pikepdf.Dictionary]:
    kids = obj.get("/K")
    values = [kids] if isinstance(kids, pikepdf.Dictionary) else (
        list(kids) if isinstance(kids, pikepdf.Array) else []
    )
    children: list[pikepdf.Dictionary] = []
    for value in values:
        if isinstance(value, pikepdf.Dictionary):
            if _is_struct_elem(value):
                children.append(value)
            else:
                children.extend(_struct_children(value))
        elif isinstance(value, pikepdf.Array):
            for nested in value:
                if isinstance(nested, pikepdf.Dictionary):
                    if _is_struct_elem(nested):
                        children.append(nested)
                    else:
                        children.extend(_struct_children(nested))
    return children


def _collect_kids(
    value: object,
    *,
    owner: pikepdf.Dictionary,
    inherited_page: pikepdf.Page | None,
    pages: dict[tuple[int, int] | int, pikepdf.Page],
    associations: dict[tuple[int, int], list[pikepdf.Dictionary]],
    unresolved: list[str],
    pageless: set[int],
    owner_label: str,
) -> None:
    if isinstance(value, Integral):
        mcid = int(value)
        if inherited_page is None:
            pageless.add(mcid)
            unresolved.append(f"{owner_label}: MCID {mcid} has no page")
        else:
            associations.setdefault((_object_key(inherited_page.obj), mcid), []).append(owner)
        return
    if isinstance(value, pikepdf.Array):
        for item in value:
            _collect_kids(
                item,
                owner=owner,
                inherited_page=inherited_page,
                pages=pages,
                associations=associations,
                unresolved=unresolved,
                pageless=pageless,
                owner_label=owner_label,
            )
        return
    if not isinstance(value, pikepdf.Dictionary) or _is_struct_elem(value):
        return
    mcid = _as_int(value.get("/MCID"))
    if mcid is not None:
        page = _page_for_ref(value.get("/Pg"), pages) or inherited_page
        if page is None:
            pageless.add(mcid)
            unresolved.append(f"{owner_label}: MCID {mcid} has no page")
        else:
            associations.setdefault((_object_key(page.obj), mcid), []).append(owner)


def _scan_structure(pdf: pikepdf.Pdf) -> _StructureScan:
    pages, page_numbers = _page_map(pdf)
    page_mcids: dict[tuple[int, int], set[int]] = {}
    for page_key, page in pages.items():
        data = _page_contents_data(page)
        if data is not None:
            page_mcids[page_key] = {int(match.group(1)) for match in _CONTENT_MCID_PATTERN.finditer(data)}

    root = pdf.Root.get("/StructTreeRoot")
    if not isinstance(root, pikepdf.Dictionary):
        return _StructureScan([], {}, [], page_mcids, page_numbers)

    elements: list[pikepdf.Dictionary] = []
    associations: dict[tuple[int, int], list[pikepdf.Dictionary]] = {}
    unresolved: list[str] = []
    pageless: set[int] = set()
    seen: set[tuple[int, int] | int] = set()

    def walk(obj: pikepdf.Dictionary, parent: pikepdf.Dictionary | None, inherited_page: pikepdf.Page | None) -> None:
        if not _is_struct_elem(obj):
            return
        key = _object_key(obj)
        if key in seen:
            return
        seen.add(key)
        elements.append(obj)
        page = _page_for_ref(obj.get("/Pg"), pages) or inherited_page
        label = f"StructElem {len(elements)}"
        _collect_kids(
            obj.get("/K"),
            owner=obj,
            inherited_page=page,
            pages=pages,
            associations=associations,
            unresolved=unresolved,
            pageless=pageless,
            owner_label=label,
        )
        for child in _struct_children(obj):
            walk(child, obj, page)

    root_kids = root.get("/K")
    if isinstance(root_kids, pikepdf.Array):
        for item in root_kids:
            if isinstance(item, pikepdf.Dictionary):
                walk(item, root, None)
    elif isinstance(root_kids, pikepdf.Dictionary):
        walk(root_kids, root, None)

    return _StructureScan(
        elements, associations, unresolved, page_mcids, page_numbers, pageless
    )


def _number_tree_pairs(node: object, seen: set[tuple[int, int] | int] | None = None) -> list[tuple[int, object]]:
    if not isinstance(node, pikepdf.Dictionary):
        return []
    if seen is None:
        seen = set()
    key = _object_key(node)
    if key in seen:
        return []
    seen.add(key)
    pairs: list[tuple[int, object]] = []
    nums = node.get("/Nums")
    if isinstance(nums, pikepdf.Array):
        for index in range(0, len(nums) - 1, 2):
            number = _as_int(nums[index])
            if number is not None:
                pairs.append((number, nums[index + 1]))
    kids = node.get("/Kids")
    if isinstance(kids, pikepdf.Array):
        for child in kids:
            pairs.extend(_number_tree_pairs(child, seen))
    return pairs


def _flatten_parent_tree(parent_tree: pikepdf.Dictionary) -> list[tuple[int, object]]:
    return _number_tree_pairs(parent_tree)


def _write_parent_tree_nums(parent_tree: pikepdf.Dictionary, pairs: list[tuple[int, object]]) -> None:
    flattened: list[object] = []
    for number, value in sorted(pairs, key=lambda pair: pair[0]):
        flattened.extend((number, value))
    parent_tree["/Nums"] = pikepdf.Array(flattened)
    if "/Kids" in parent_tree:
        del parent_tree["/Kids"]
    if pairs:
        parent_tree["/Limits"] = pikepdf.Array([min(number for number, _ in pairs), max(number for number, _ in pairs)])


def _parent_tree_array(
    parent_tree: pikepdf.Dictionary,
    key: int,
    *,
    actions: list[str],
) -> tuple[pikepdf.Array | None, bool]:
    pairs = _flatten_parent_tree(parent_tree)
    for existing_key, value in pairs:
        if existing_key == key:
            return (
                value if isinstance(value, pikepdf.Array) else None,
                False,
            )
    array = pikepdf.Array()
    pairs.append((key, array))
    _write_parent_tree_nums(parent_tree, pairs)
    actions.append(f"ParentTree: added page index {key}")
    return array, True


def _content_unresolved(
    page_mcids: dict[tuple[int, int], set[int]],
    associations: dict[tuple[int, int], list[pikepdf.Dictionary]],
    page_numbers: dict[tuple[int, int], int],
) -> list[str]:
    unresolved: list[str] = []
    for page_key, mcids in page_mcids.items():
        for mcid in sorted(mcids):
            if (page_key, mcid) not in associations:
                unresolved.append(f"page {page_numbers[page_key]} MCID {mcid}")
    return unresolved


def _diagnostics_for_pdf(pdf: pikepdf.Pdf) -> TaggedContentDiagnostics:
    scan = _scan_structure(pdf)
    root = pdf.Root.get("/StructTreeRoot")
    parent_tree = root.get("/ParentTree") if isinstance(root, pikepdf.Dictionary) else None
    mapped_pages = {
        page_key
        for page_key, mcid in scan.associations
        if mcid in scan.page_mcids.get(page_key, set())
    }
    mapped_count = sum(
        1
        for page_key, mcid in scan.associations
        if mcid in scan.page_mcids.get(page_key, set())
    )
    return TaggedContentDiagnostics(
        struct_tree_root_present=isinstance(root, pikepdf.Dictionary),
        parent_tree_present=isinstance(parent_tree, pikepdf.Dictionary),
        pages_with_struct_parents=sum(
            1 for page in pdf.pages if _as_int(page.get("/StructParents")) is not None
        ),
        pages_with_mapped_mcids=len(mapped_pages),
        mapped_mcid_count=mapped_count,
        unresolved_mcids=_content_unresolved(
            scan.page_mcids, scan.associations, scan.page_numbers
        ),
        parent_tree_keys=(
            sorted({number for number, _ in _flatten_parent_tree(parent_tree)})
            if isinstance(parent_tree, pikepdf.Dictionary)
            else []
        ),
    )


def collect_tagged_content_diagnostics(pdf: pikepdf.Pdf) -> TaggedContentDiagnostics:
    """Return nonblocking structure facts for an already-open PDF."""
    return _diagnostics_for_pdf(pdf)


def inspect_tagged_content(pdf_bytes: bytes) -> TaggedContentDiagnostics:
    """Return nonblocking structure facts for PDF bytes."""
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        return _diagnostics_for_pdf(pdf)


# Marked-content tags an orphan block can be adopted under, mapped to the
# structure type of the element that adopts it. Only /P is here. An orphan
# /Span is left to the ActualText stage, which decodes list labels through the
# page fonts and retags decorative Spans as artifacts; adopting one would take
# it out of that stage's view and lose the label handling. The BDC scanner
# these candidates come from (_iter_bdc_mcids_on_page) never reports a heading
# tag, so there is nothing to map for /H1 to /H6.
_ADOPTABLE_ORPHAN_TAGS = {"P": "/P"}

# Structure types whose content model accepts a paragraph. Grouping elements
# do; a list, a table or a row does not, and neither does a paragraph or a
# heading. Adobe's list and table rules only check the other direction, so a
# stray /P between two /TR would pass the checker and still leave a reader's
# table navigation with a paragraph where a row belongs.
_PARAGRAPH_CONTAINERS = {
    "/Document",
    "/DocumentFragment",
    "/Part",
    "/Art",
    "/Sect",
    "/Div",
    "/BlockQuote",
    "/Caption",
    "/NonStruct",
    "/Private",
    "/Aside",
    "/LBody",
}


def _resolved_role(
    node: pikepdf.Dictionary, root: pikepdf.Dictionary
) -> str | None:
    """A node's structure type, following /RoleMap to a standard type."""
    stype = node.get("/S")
    if stype is None:
        return None
    name = str(stype)
    role_map = root.get("/RoleMap")
    seen: set[str] = set()
    while isinstance(role_map, pikepdf.Dictionary) and name not in seen:
        seen.add(name)
        mapped = role_map.get(name)
        if mapped is None:
            break
        name = str(mapped)
    return name


def _shallow_mcids(
    value: object,
    page: pikepdf.Page | None,
    pages: dict[tuple[int, int] | int, pikepdf.Page],
    out: set[tuple[tuple[int, int] | int, int]],
) -> None:
    if isinstance(value, Integral):
        if page is not None:
            out.add((_object_key(page.obj), int(value)))
        return
    if isinstance(value, pikepdf.Array):
        for item in value:
            _shallow_mcids(item, page, pages, out)
        return
    if not isinstance(value, pikepdf.Dictionary) or _is_struct_elem(value):
        return
    mcid = _as_int(value.get("/MCID"))
    if mcid is None:
        return
    target = _page_for_ref(value.get("/Pg"), pages) or page
    if target is not None:
        out.add((_object_key(target.obj), mcid))


def _structure_index(
    root: pikepdf.Dictionary,
    pages: dict[tuple[int, int] | int, pikepdf.Page],
) -> tuple[
    dict[tuple[int, int] | int, pikepdf.Dictionary | None],
    dict[tuple[int, int] | int, set[tuple[tuple[int, int] | int, int]]],
]:
    """Map every structure node to its parent and to the MCIDs beneath it."""
    parents: dict[tuple[int, int] | int, pikepdf.Dictionary | None] = {}
    subtree: dict[tuple[int, int] | int, set[tuple[tuple[int, int] | int, int]]] = {}

    def walk(
        obj: pikepdf.Dictionary,
        parent: pikepdf.Dictionary | None,
        inherited_page: pikepdf.Page | None,
    ) -> set[tuple[tuple[int, int] | int, int]]:
        key = _object_key(obj)
        if key in subtree:
            return subtree[key]
        subtree[key] = set()
        parents[key] = parent
        page = _page_for_ref(obj.get("/Pg"), pages) or inherited_page
        found: set[tuple[tuple[int, int] | int, int]] = set()
        _shallow_mcids(obj.get("/K"), page, pages, found)
        for child in _struct_children(obj):
            found |= walk(child, obj, page)
        subtree[key] = found
        return found

    walk(root, None, None)
    return parents, subtree


def _has_bare_mcid(value: object) -> bool:
    if isinstance(value, Integral):
        return True
    if isinstance(value, pikepdf.Array):
        return any(_has_bare_mcid(item) for item in value)
    return False


def _entry_is_stale(
    entry: object,
    page_key: tuple[int, int] | int,
    mcid: int,
    pages: dict[tuple[int, int] | int, pikepdf.Page],
) -> bool:
    """Whether a ParentTree entry names an element that never references it.

    A producer that numbers marked content it never tags leaves the array
    short by those slots, so every entry after the gap names the element of
    a different MCID. The element's own /K settles it: an entry whose
    element points somewhere else is stale, not an alternative reading.
    """
    if not isinstance(entry, pikepdf.Dictionary) or not _is_struct_elem(entry):
        return False
    page = _page_for_ref(entry.get("/Pg"), pages)
    kids = entry.get("/K")
    if page is None and _has_bare_mcid(kids):
        # Without a page the bare MCIDs cannot be placed, so leave it alone.
        return False
    references: set[tuple[tuple[int, int] | int, int]] = set()
    _shallow_mcids(kids, page, pages, references)
    if not references:
        return False
    return (page_key, mcid) not in references


def _neighbour_mcids(
    kid: object,
    page_mcids: Callable[[object], set[int]],
) -> set[int] | None:
    """MCIDs a sibling covers, or None when the sibling cannot be compared."""
    if not isinstance(kid, pikepdf.Dictionary) or not _is_struct_elem(kid):
        # A bare MCID or an MCR beside the insertion point has no subtree to
        # compare, so the orphan's place among these kids is a guess.
        return None
    return page_mcids(kid)


def _orphan_placement(
    mcid: int,
    *,
    page_key: tuple[int, int] | int,
    owners: dict[int, pikepdf.Dictionary],
    parents: dict[tuple[int, int] | int, pikepdf.Dictionary | None],
    subtree: dict[tuple[int, int] | int, set[tuple[tuple[int, int] | int, int]]],
    root: pikepdf.Dictionary,
) -> tuple[pikepdf.Dictionary, int] | None:
    """Find the element and child index where an orphan MCID belongs.

    Position comes from the MCIDs already in the tree: the block follows the
    highest MCID before it and precedes the lowest one after it. A whole
    document is rarely in MCID order, so only the two neighbours at the
    insertion point have to bracket the orphan. When they do not, there is
    nothing to infer and the block is left for review.
    """

    def page_mcids(node: object) -> set[int]:
        if not isinstance(node, pikepdf.Dictionary):
            return set()
        return {
            number
            for key, number in subtree.get(_object_key(node), set())
            if key == page_key
        }

    before = [number for number in owners if number < mcid]
    after = [number for number in owners if number > mcid]
    if before:
        anchor = owners[max(before)]
        offset = 1
    elif after:
        anchor = owners[min(after)]
        offset = 0
    else:
        return None

    while True:
        parent = parents.get(_object_key(anchor))
        if parent is None or _same_object(parent, root):
            break
        grandparent = parents.get(_object_key(parent))
        if grandparent is None or _same_object(grandparent, root):
            # Climbing onto a top-level element would put the new element
            # beside /Document rather than in it, and a reader that walks
            # the tree then finds no common ancestor for the page's content.
            break
        covered = page_mcids(parent)
        if not covered:
            break
        if offset == 1 and max(covered) < mcid:
            anchor = parent
            continue
        if offset == 0 and min(covered) > mcid:
            anchor = parent
            continue
        break

    container = parents.get(_object_key(anchor))
    if container is None or _same_object(container, root):
        # A kid of the structure tree root is not inside the element the rest
        # of the page hangs from, and a reader that walks the tree then finds
        # no common ancestor for the page's content.
        return None
    if _resolved_role(container, root) not in _PARAGRAPH_CONTAINERS:
        return None
    kids = container.get("/K")
    if isinstance(kids, pikepdf.Dictionary):
        # A lone element kid, which the caller turns into an array to insert
        # beside. Nothing is written until the placement is settled.
        if not _same_object(kids, anchor):
            return None
        kids = pikepdf.Array([kids])
    if not isinstance(kids, pikepdf.Array):
        return None
    index = next(
        (
            position
            for position, kid in enumerate(kids)
            if _same_object(kid, anchor)
        ),
        None,
    )
    if index is None:
        return None
    insert_at = index + offset
    for position in range(insert_at - 1, -1, -1):
        covered = _neighbour_mcids(kids[position], page_mcids)
        if covered is None:
            return None
        if covered:
            if max(covered) > mcid:
                return None
            break
    for position in range(insert_at, len(kids)):
        covered = _neighbour_mcids(kids[position], page_mcids)
        if covered is None:
            return None
        if covered:
            if min(covered) < mcid:
                return None
            break
    return container, insert_at


def _adopt_orphan_marked_content(
    pdf: pikepdf.Pdf,
    root: pikepdf.Dictionary,
    scan: _StructureScan,
    *,
    actions: list[str],
    conflicts: list[str],
) -> int:
    """Put marked-content blocks that show text but sit outside the tree into it.

    Adobe reads such a block as an untagged element and fails "Other elements
    alternate text" on it, and a screen reader never speaks it. Silencing it
    with /ActualText would hide real text, so the block is adopted as its own
    element at the position its MCID gives it.
    """
    pages, page_numbers = _page_map(pdf)
    parents, subtree = _structure_index(root, pages)
    adopted = 0
    for page_key, page in pages.items():
        data = _page_contents_data(page)
        if data is None:
            continue
        owners: dict[int, pikepdf.Dictionary] = {
            mcid: found[0]
            for (key, mcid), found in scan.associations.items()
            if key == page_key and found
        }
        candidates = [
            (mcid, tag)
            for mcid, tag in _iter_bdc_mcids_on_page(data)
            if mcid not in owners
            and mcid not in scan.pageless_mcids
            and tag in _ADOPTABLE_ORPHAN_TAGS
        ]
        font_maps = _page_font_code_maps(page)
        for mcid, tag in sorted(candidates):
            block = _get_mcid_block_to_emc(data, mcid)
            if block is None or not _body_has_show_ops(block[2]):
                # Nothing here is spoken, so this is the ActualText sweep's
                # call between decoration and an artifact, not a tree repair.
                continue
            if _body_paints_no_content(
                block[2],
                font_code_maps=font_maps,
                initial_font=_font_in_effect_at(data, block[0]),
            ):
                # Shows only whitespace. Adopting it would add an empty
                # paragraph; the ActualText stage retags blocks like this.
                continue
            placement = _orphan_placement(
                mcid,
                page_key=page_key,
                owners=owners,
                parents=parents,
                subtree=subtree,
                root=root,
            )
            if placement is None:
                conflicts.append(
                    f"page {page_numbers[page_key]} MCID {mcid}: "
                    "orphan text block has no inferable position"
                )
                continue
            container, insert_at = placement
            struct_type = _ADOPTABLE_ORPHAN_TAGS[tag]
            element = pdf.make_indirect(
                pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name(struct_type),
                        "/P": container,
                        "/Pg": page.obj,
                        "/K": mcid,
                    }
                )
            )
            existing = container.get("/K")
            kids = (
                [existing]
                if isinstance(existing, pikepdf.Dictionary)
                else list(existing)
            )
            kids.insert(insert_at, element)
            container["/K"] = pikepdf.Array(kids)

            key = _object_key(element)
            parents[key] = container
            subtree[key] = {(page_key, mcid)}
            node: pikepdf.Dictionary | None = container
            while node is not None:
                subtree.setdefault(_object_key(node), set()).add((page_key, mcid))
                node = parents.get(_object_key(node))
            owners[mcid] = element
            # Not added to scan.elements: struct_elements_found counts what
            # the file arrived with.
            scan.associations.setdefault((page_key, mcid), []).append(element)
            adopted += 1
            actions.append(
                f"StructElem: adopted page {page_numbers[page_key]} MCID {mcid} "
                f"as {struct_type} from an orphan /{tag} block"
            )
    return adopted


def repair_tagged_content(
    pdf_bytes: bytes,
) -> tuple[bytes, TaggedContentRepairResult]:
    """Repair only tagged-content relationships derivable from existing objects."""
    actions: list[str] = []
    conflicts: list[str] = []
    changed = False
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        pages_found = len(pdf.pages)
        root = pdf.Root.get("/StructTreeRoot")
        if not isinstance(root, pikepdf.Dictionary):
            scan = _scan_structure(pdf)
            unresolved = _content_unresolved(
                scan.page_mcids, scan.associations, scan.page_numbers
            )
            result = TaggedContentRepairResult(
                pages_found=pages_found,
                struct_elements_found=0,
                pages_updated=0,
                struct_elements_updated=0,
                parent_tree_entries_added=0,
                parent_tree_entries_updated=0,
                parent_tree_next_key_updated=False,
                mapped_mcids=0,
                unresolved_mcids=unresolved,
                unresolved_associations=[],
                conflicts=[],
                actions=[],
            )
            return pdf_bytes, result

        scan = _scan_structure(pdf)
        struct_elements_updated = 0
        for element in scan.elements:
            if element.get("/P") is not None:
                continue
            parent: pikepdf.Dictionary | None = None

            def find_parent(obj: pikepdf.Dictionary) -> pikepdf.Dictionary | None:
                for child in _struct_children(obj):
                    if _same_object(child, element):
                        return obj
                    found = find_parent(child)
                    if found is not None:
                        return found
                return None

            parent = find_parent(root)
            if parent is not None:
                element["/P"] = parent
                struct_elements_updated += 1
                changed = True
                actions.append(f"StructElem: restored missing /P link on element {struct_elements_updated}")

        struct_elements_added = _adopt_orphan_marked_content(
            pdf, root, scan, actions=actions, conflicts=conflicts
        )
        if struct_elements_added:
            changed = True

        page_lookup, page_numbers = _page_map(pdf)
        page_associations: dict[tuple[int, int], list[pikepdf.Dictionary]] = scan.associations
        page_keys: dict[tuple[int, int] | int, int] = {}
        key_to_pages: dict[int, list[int]] = {}
        for page_number, page in enumerate(pdf.pages, start=1):
            value = _as_int(page.get("/StructParents"))
            if value is not None:
                page_key = _object_key(page.obj)
                page_keys[page_key] = value
                key_to_pages.setdefault(value, []).append(page_number)
        for key, numbers in key_to_pages.items():
            if len(numbers) > 1:
                conflicts.append(
                    f"StructParents key {key} is shared by pages {numbers}"
                )

        parent_tree = root.get("/ParentTree")
        parent_tree_invalid = (
            parent_tree is not None and not isinstance(parent_tree, pikepdf.Dictionary)
        )
        if parent_tree_invalid:
            conflicts.append("StructTreeRoot /ParentTree is not a dictionary")
            parent_tree = None

        existing_pairs = _flatten_parent_tree(parent_tree) if parent_tree is not None else []
        existing_values: dict[int, object] = {}
        for key, value in existing_pairs:
            if key in existing_values and not _same_object(existing_values[key], value):
                conflicts.append(f"ParentTree key {key} has conflicting duplicate entries")
            else:
                existing_values.setdefault(key, value)

        association_page_keys: dict[tuple[int, int], int] = {}
        next_key = max(
            [key for key, _ in existing_pairs]
            + list(page_keys.values())
            + [_as_int(root.get("/ParentTreeNextKey")) or 0]
        )
        for (page_key, mcid), owners in sorted(page_associations.items(), key=lambda item: item[0][1]):
            page_number = page_numbers.get(page_key)
            if page_number is None:
                scan.unresolved_associations.append(f"MCID {mcid}: referenced page is not in document")
                continue
            if len({ _object_key(owner) for owner in owners }) > 1:
                conflicts.append(
                    f"page {page_number} MCID {mcid} is linked to multiple StructElem nodes"
                )
            if page_key in page_keys and len(key_to_pages[page_keys[page_key]]) > 1:
                scan.unresolved_associations.append(
                    f"page {page_number} MCID {mcid}: shared /StructParents key"
                )
                continue
            if page_key not in page_keys:
                if parent_tree_invalid:
                    scan.unresolved_associations.append(
                        f"page {page_number} MCID {mcid}: invalid ParentTree prevents repair"
                    )
                    continue
                page_keys[page_key] = next_key
                key_to_pages.setdefault(next_key, []).append(page_number)
                next_key += 1
                page_lookup[page_key]["/StructParents"] = page_keys[page_key]
                changed = True
                actions.append(
                    f"page {page_number}: set /StructParents to {page_keys[page_key]}"
                )
            association_page_keys[(page_key, mcid)] = page_keys[page_key]

        parent_tree_entries_added = 0
        parent_tree_entries_remapped = 0
        if page_associations:
            if parent_tree is None and root.get("/ParentTree") is None:
                parent_tree = pdf.make_indirect(
                    pikepdf.Dictionary({"/Nums": pikepdf.Array()})
                )
                root["/ParentTree"] = parent_tree
                changed = True
                actions.append("StructTreeRoot: created /ParentTree")
            if parent_tree is not None:
                arrays: dict[int, pikepdf.Array] = {}
                for key in sorted(set(association_page_keys.values())):
                    array, array_added = _parent_tree_array(
                        parent_tree,
                        key,
                        actions=actions,
                    )
                    if array is None:
                        conflicts.append(f"ParentTree key {key} is not an array")
                        continue
                    changed = changed or array_added
                    arrays[key] = array
                for (page_key, mcid), owners in page_associations.items():
                    key = association_page_keys.get((page_key, mcid))
                    if key is None or key not in arrays:
                        scan.unresolved_associations.append(
                            f"page {page_numbers.get(page_key, '?')} MCID {mcid}: no ParentTree array"
                        )
                        continue
                    array = arrays[key]
                    while len(array) <= mcid:
                        array.append(None)
                    owner = owners[0]
                    current = array[mcid]
                    if current is None:
                        array[mcid] = owner
                        parent_tree_entries_added += 1
                        changed = True
                        actions.append(
                            f"ParentTree: mapped page {page_numbers[page_key]} MCID {mcid}"
                        )
                    elif not _same_object(current, owner):
                        if _entry_is_stale(current, page_key, mcid, page_lookup):
                            array[mcid] = owner
                            parent_tree_entries_remapped += 1
                            changed = True
                            actions.append(
                                f"ParentTree: remapped page {page_numbers[page_key]} "
                                f"MCID {mcid} to the element that references it"
                            )
                        else:
                            conflicts.append(
                                f"page {page_numbers[page_key]} MCID {mcid}: existing ParentTree mapping preserved"
                            )
                            scan.unresolved_associations.append(
                                f"page {page_numbers[page_key]} MCID {mcid}: ParentTree conflict"
                            )

        if parent_tree is not None:
            existing_parent_keys = [
                key for key, _ in _flatten_parent_tree(parent_tree)
            ]
            required_next_key = (
                max(existing_parent_keys + list(page_keys.values())) + 1
                if existing_parent_keys or page_keys
                else 0
            )
            current_next_key = _as_int(root.get("/ParentTreeNextKey"))
            if current_next_key is None or current_next_key < required_next_key:
                root["/ParentTreeNextKey"] = required_next_key
                changed = True
                actions.append(
                    f"StructTreeRoot: set /ParentTreeNextKey to {required_next_key}"
                )

        unresolved_mcids = _content_unresolved(
            scan.page_mcids, page_associations, scan.page_numbers
        )
        unresolved_associations = list(dict.fromkeys(scan.unresolved_associations))
        mapped_mcids = sum(
            1
            for page_key, mcid in page_associations
            if mcid in scan.page_mcids.get(page_key, set())
        )
        # Count page updates from actions to avoid treating pre-existing keys as repairs.
        pages_updated = sum(1 for action in actions if action.startswith("page ") and "set /StructParents" in action)
        parent_tree_next_key_updated = any(
            "set /ParentTreeNextKey" in action for action in actions
        )
        if not changed:
            return pdf_bytes, TaggedContentRepairResult(
                pages_found,
                len(scan.elements),
                pages_updated,
                struct_elements_updated,
                parent_tree_entries_added,
                parent_tree_entries_remapped,
                parent_tree_next_key_updated,
                mapped_mcids,
                unresolved_mcids,
                unresolved_associations,
                conflicts,
                actions,
                struct_elements_added,
            )
        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue(), TaggedContentRepairResult(
            pages_found,
            len(scan.elements),
            pages_updated,
            struct_elements_updated,
            parent_tree_entries_added,
            parent_tree_entries_remapped,
            parent_tree_next_key_updated,
            mapped_mcids,
            unresolved_mcids,
            unresolved_associations,
            conflicts,
            actions,
            struct_elements_added,
        )
