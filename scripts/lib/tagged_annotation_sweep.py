"""Repair objectively derivable tagged-PDF annotation relationships."""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass, field
from numbers import Integral

import pikepdf

from lib.tagged_content_sweep import (
    _flatten_parent_tree,
    _object_key,
    _same_object,
    _write_parent_tree_nums,
)

_NON_CONTENT_SUBTYPES = frozenset({"/Popup", "/PrinterMark", "/TrapNet"})


@dataclass
class TaggedAnnotationRepairResult:
    """Facts and nonblocking diagnostics produced by the annotation sweep."""

    pages_found: int
    annotations_found: int
    supported_annotations: int
    annotations_repaired: int
    struct_elements_created: int
    objr_created: int
    parent_tree_entries_added: int
    parent_tree_entries_updated: int
    parent_tree_next_key_updated: bool
    skipped: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def annotations_updated(self) -> int:
        """Compatibility alias for callers using an update-oriented name."""
        return self.annotations_repaired

    @property
    def diagnostics(self) -> list[str]:
        return self.skipped + self.unresolved + self.conflicts


@dataclass
class _AnnotationOccurrence:
    page: pikepdf.Page
    page_number: int
    annotation: pikepdf.Dictionary


@dataclass
class _ObjectReference:
    owner: pikepdf.Dictionary
    objr: pikepdf.Dictionary
    page: pikepdf.Page | None


def _as_int(value: object) -> int | None:
    return int(value) if isinstance(value, Integral) else None


def _is_struct_elem(value: object) -> bool:
    return isinstance(value, pikepdf.Dictionary) and (
        str(value.get("/Type")) == "/StructElem" or value.get("/S") is not None
    )


def _is_objr(value: object) -> bool:
    return isinstance(value, pikepdf.Dictionary) and str(value.get("/Type")) == "/OBJR"


def _page_map(
    pdf: pikepdf.Pdf,
) -> dict[tuple[int, int] | int, pikepdf.Page]:
    pages: dict[tuple[int, int] | int, pikepdf.Page] = {}
    for page in pdf.pages:
        key = _object_key(page.obj)
        pages[key] = page
    return pages


def _page_for_reference(
    reference: object,
    pages: dict[tuple[int, int] | int, pikepdf.Page],
) -> pikepdf.Page | None:
    if reference is None:
        return None
    return pages.get(_object_key(reference))


def _annotation_subtype(annotation: pikepdf.Dictionary) -> str | None:
    value = annotation.get("/Subtype")
    if value is None:
        return None
    subtype = str(value)
    return subtype if subtype.startswith("/") else None


def _annotation_role(subtype: str) -> str:
    if subtype == "/Link":
        return "/Link"
    if subtype == "/Widget":
        return "/Form"
    return "/Annot"


def _annotation_entries(
    pdf: pikepdf.Pdf,
    *,
    skipped: list[str],
) -> tuple[
    dict[tuple[int, int] | int, list[_AnnotationOccurrence]],
    int,
    int,
]:
    occurrences: dict[tuple[int, int] | int, list[_AnnotationOccurrence]] = {}
    annotations_found = 0
    supported_annotations = 0
    for page_number, page in enumerate(pdf.pages, start=1):
        raw_annots = page.get("/Annots")
        if raw_annots is None:
            continue
        if isinstance(raw_annots, pikepdf.Array):
            annots = list(raw_annots)
        elif isinstance(raw_annots, pikepdf.Dictionary):
            annots = [raw_annots]
        else:
            skipped.append(f"page {page_number}: malformed /Annots entry")
            continue
        for index, annotation in enumerate(annots, start=1):
            annotations_found += 1
            if not isinstance(annotation, pikepdf.Dictionary):
                skipped.append(
                    f"page {page_number} annotation {index}: malformed annotation entry"
                )
                continue
            subtype = _annotation_subtype(annotation)
            if subtype is None:
                skipped.append(
                    f"page {page_number} annotation {index}: missing or malformed /Subtype"
                )
                continue
            if subtype in _NON_CONTENT_SUBTYPES:
                skipped.append(
                    f"page {page_number} annotation {index}: skipped non-content {subtype}"
                )
                continue
            if (
                annotation.get("/StructParent") is not None
                and _as_int(annotation.get("/StructParent")) is None
            ):
                skipped.append(
                    f"page {page_number} annotation {index}: malformed /StructParent"
                )
                continue
            supported_annotations += 1
            key = _object_key(annotation)
            occurrences.setdefault(key, []).append(
                _AnnotationOccurrence(page, page_number, annotation)
            )
    return occurrences, annotations_found, supported_annotations


def _number_tree_scan(
    node: object,
    *,
    pairs: list[tuple[int, object]],
    seen: set[tuple[int, int] | int],
    issues: list[str],
) -> None:
    if not isinstance(node, pikepdf.Dictionary):
        issues.append("ParentTree number-tree node is not a dictionary")
        return
    key = _object_key(node)
    if key in seen:
        issues.append("ParentTree number tree contains a cycle or repeated node")
        return
    seen.add(key)
    nums = node.get("/Nums")
    kids = node.get("/Kids")
    if nums is not None and kids is not None:
        issues.append("ParentTree number-tree node has both /Nums and /Kids")
    if nums is not None:
        if not isinstance(nums, pikepdf.Array):
            issues.append("ParentTree /Nums is not an array")
        elif len(nums) % 2:
            issues.append("ParentTree /Nums has an unmatched key or value")
        else:
            for index in range(0, len(nums), 2):
                number = _as_int(nums[index])
                if number is None:
                    issues.append("ParentTree /Nums contains a non-integer key")
                elif number < 0:
                    issues.append("ParentTree /Nums contains a negative key")
                else:
                    pairs.append((number, nums[index + 1]))
    if kids is not None:
        if not isinstance(kids, pikepdf.Array):
            issues.append("ParentTree /Kids is not an array")
        else:
            for child in kids:
                if not isinstance(child, pikepdf.Dictionary):
                    issues.append("ParentTree /Kids contains a non-dictionary node")
                    continue
                _number_tree_scan(child, pairs=pairs, seen=seen, issues=issues)


def _scan_parent_tree(
    parent_tree: pikepdf.Dictionary,
) -> tuple[list[tuple[int, object]], dict[int, object], list[str]]:
    pairs: list[tuple[int, object]] = []
    issues: list[str] = []
    _number_tree_scan(parent_tree, pairs=pairs, seen=set(), issues=issues)
    values: dict[int, object] = {}
    for key, value in pairs:
        if key in values and not _same_object(values[key], value):
            issues.append(f"ParentTree key {key} has conflicting duplicate entries")
        else:
            values.setdefault(key, value)
    return pairs, values, issues


def _struct_kids(value: object) -> list[object]:
    if isinstance(value, pikepdf.Array):
        return list(value)
    if isinstance(value, pikepdf.Dictionary):
        return [value]
    return []


def _scan_structure(
    root: pikepdf.Dictionary,
    *,
    pages: dict[tuple[int, int] | int, pikepdf.Page],
    occurrences: dict[tuple[int, int] | int, list[_AnnotationOccurrence]],
    conflicts: list[str],
) -> tuple[
    list[pikepdf.Dictionary],
    dict[tuple[int, int] | int, list[_ObjectReference]],
    dict[tuple[int, int] | int, pikepdf.Page | None],
]:
    elements: list[pikepdf.Dictionary] = []
    objr_by_annotation: dict[tuple[int, int] | int, list[_ObjectReference]] = {}
    element_pages: dict[tuple[int, int] | int, pikepdf.Page | None] = {}
    seen_elements: set[tuple[int, int] | int] = set()
    seen_objrs: set[tuple[int, int] | int] = set()

    def walk(element: pikepdf.Dictionary, inherited_page: pikepdf.Page | None) -> None:
        element_key = _object_key(element)
        if element_key in seen_elements:
            return
        seen_elements.add(element_key)
        elements.append(element)
        if element.get("/S") is None:
            conflicts.append("structure element is missing its /S role")
        raw_page = element.get("/Pg")
        if raw_page is not None and _page_for_reference(raw_page, pages) is None:
            conflicts.append(
                "structure element has a /Pg reference outside the page tree"
            )
            element_page = None
        else:
            element_page = _page_for_reference(raw_page, pages) or inherited_page
        element_pages[element_key] = element_page
        for child in _struct_kids(element.get("/K")):
            if _is_objr(child):
                objr_key = _object_key(child)
                if objr_key in seen_objrs:
                    conflicts.append("structure tree repeats an /OBJR reference")
                    continue
                seen_objrs.add(objr_key)
                annotation = child.get("/Obj")
                if not isinstance(annotation, pikepdf.Dictionary):
                    conflicts.append("structure /OBJR is missing a dictionary /Obj")
                    continue
                annotation_key = _object_key(annotation)
                if annotation_key not in occurrences:
                    conflicts.append(
                        "structure /OBJR /Obj is not a supported annotation on a page"
                    )
                    continue
                raw_objr_page = child.get("/Pg")
                if (
                    raw_objr_page is not None
                    and _page_for_reference(raw_objr_page, pages) is None
                ):
                    conflicts.append("structure /OBJR has a /Pg outside the page tree")
                    objr_page = None
                else:
                    objr_page = (
                        _page_for_reference(raw_objr_page, pages) or element_page
                    )
                if objr_page is None:
                    conflicts.append("structure /OBJR cannot resolve its page")
                for occurrence in occurrences[annotation_key]:
                    if objr_page is not None and not _same_object(
                        objr_page.obj, occurrence.page.obj
                    ):
                        conflicts.append(
                            f"page {occurrence.page_number}: structure /OBJR page "
                            "does not match annotation page"
                        )
                objr_by_annotation.setdefault(annotation_key, []).append(
                    _ObjectReference(element, child, objr_page)
                )
                continue
            if isinstance(child, pikepdf.Dictionary) and _is_struct_elem(child):
                walk(child, element_page)

    root_kids = root.get("/K")
    if root_kids is not None and not isinstance(
        root_kids, (pikepdf.Array, pikepdf.Dictionary)
    ):
        conflicts.append("StructTreeRoot /K is neither an array nor a dictionary")
    for child in _struct_kids(root_kids):
        if not isinstance(child, pikepdf.Dictionary) or not _is_struct_elem(child):
            conflicts.append("StructTreeRoot /K contains a non-structure-element child")
            continue
        walk(child, None)
    return elements, objr_by_annotation, element_pages


def _result(
    *,
    pages_found: int,
    annotations_found: int,
    supported_annotations: int,
    skipped: list[str] | None = None,
    unresolved: list[str] | None = None,
    conflicts: list[str] | None = None,
    actions: list[str] | None = None,
    annotations_repaired: int = 0,
    struct_elements_created: int = 0,
    objr_created: int = 0,
    parent_tree_entries_added: int = 0,
    parent_tree_entries_updated: int = 0,
    parent_tree_next_key_updated: bool = False,
) -> TaggedAnnotationRepairResult:
    return TaggedAnnotationRepairResult(
        pages_found=pages_found,
        annotations_found=annotations_found,
        supported_annotations=supported_annotations,
        annotations_repaired=annotations_repaired,
        struct_elements_created=struct_elements_created,
        objr_created=objr_created,
        parent_tree_entries_added=parent_tree_entries_added,
        parent_tree_entries_updated=parent_tree_entries_updated,
        parent_tree_next_key_updated=parent_tree_next_key_updated,
        skipped=list(dict.fromkeys(skipped or [])),
        unresolved=list(dict.fromkeys(unresolved or [])),
        conflicts=list(dict.fromkeys(conflicts or [])),
        actions=actions or [],
    )


def _reachable_struct_objgens(root: pikepdf.Dictionary) -> set[tuple[int, int]]:
    reachable: set[tuple[int, int]] = set()
    stack: list[pikepdf.Dictionary] = [root]
    while stack:
        obj = stack.pop()
        objgen = obj.objgen
        if objgen in reachable:
            continue
        reachable.add(objgen)
        kids = obj.get("/K")
        items = list(kids) if isinstance(kids, pikepdf.Array) else [kids]
        for item in items:
            if isinstance(item, pikepdf.Dictionary) and item.get("/S") is not None:
                stack.append(item)
    return reachable


def _parent_tree_value_objgens(
    parent_tree: pikepdf.Dictionary,
) -> set[tuple[int, int]]:
    objgens: set[tuple[int, int]] = set()

    def record(value: object) -> None:
        if isinstance(value, pikepdf.Dictionary):
            objgens.add(value.objgen)
        elif isinstance(value, pikepdf.Array):
            for item in value:
                if isinstance(item, pikepdf.Dictionary):
                    objgens.add(item.objgen)

    def walk(node: pikepdf.Dictionary) -> None:
        nums = node.get("/Nums")
        if isinstance(nums, pikepdf.Array):
            items = list(nums)
            for index in range(1, len(items), 2):
                record(items[index])
        kids = node.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                if isinstance(kid, pikepdf.Dictionary):
                    walk(kid)

    walk(parent_tree)
    return objgens


def _subtree_objgens(obj: pikepdf.Dictionary) -> set[tuple[int, int]]:
    seen: set[tuple[int, int]] = set()
    stack = [obj]
    while stack:
        node = stack.pop()
        if node.objgen in seen:
            continue
        seen.add(node.objgen)
        kids = node.get("/K")
        items = list(kids) if isinstance(kids, pikepdf.Array) else [kids]
        for item in items:
            if isinstance(item, pikepdf.Dictionary) and item.get("/S") is not None:
                stack.append(item)
    return seen


_RECONNECT_ALLOWED_PARENTS = {
    "/TR": {"/Table", "/THead", "/TBody", "/TFoot"},
    "/TD": {"/TR"},
    "/TH": {"/TR"},
    "/THead": {"/Table"},
    "/TBody": {"/Table"},
    "/TFoot": {"/Table"},
    "/LI": {"/L"},
    "/Lbl": {"/LI"},
    "/LBody": {"/LI"},
}


def _reconnect_orphaned_parenttree_subtrees(
    pdf: pikepdf.Pdf,
    root: pikepdf.Dictionary,
    parent_tree: pikepdf.Dictionary,
    actions: list[str],
) -> int:
    """Reattach live structure subtrees whose parent down-link was lost.

    Some producers corrupt one /K down-link (e.g. writing a bare number where
    a reference belongs), leaving a fully-formed subtree — consistent /P
    up-links, /OBJR, ParentTree entries — unreachable from the root, which
    fails Adobe's tagged-annotations check for the annotations inside it.
    Reattach an unreachable element only on strong evidence of intent: its /P
    names a parent that is reachable, and its subtree is referenced by the
    ParentTree (the author registered it as live content). Object numbers are
    never compared: pikepdf renumbers objects on every save.
    """
    live_refs = _parent_tree_value_objgens(parent_tree)
    reconnected = 0
    for _round in range(8):
        reachable = _reachable_struct_objgens(root)
        appended = False
        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Dictionary):
                continue
            if obj.objgen in reachable:
                continue
            if obj.get("/S") is None or not _is_struct_elem(obj):
                continue
            parent = obj.get("/P")
            if not isinstance(parent, pikepdf.Dictionary):
                continue
            if parent.objgen not in reachable:
                continue
            allowed_parents = _RECONNECT_ALLOWED_PARENTS.get(str(obj.get("/S")))
            if allowed_parents is not None and (
                str(parent.get("/S")) not in allowed_parents
            ):
                # The recorded /P itself violates the child's required
                # nesting (e.g. a /TR whose parent is a /Sect); reattaching
                # would trade an ignored subtree for an invalid one.
                continue
            if not (_subtree_objgens(obj) & live_refs):
                continue
            kids = parent.get("/K")
            if isinstance(kids, pikepdf.Array):
                kids.append(obj)
            elif kids is None:
                parent["/K"] = obj
            else:
                parent["/K"] = pikepdf.Array([kids, obj])
            reconnected += 1
            appended = True
            actions.append(
                f"reconnected unreachable {obj.get('/S')} subtree (registered "
                f"in ParentTree) under its parent {parent.get('/S')}"
            )
        if not appended:
            break
    return reconnected


def repair_tagged_annotations(
    pdf_bytes: bytes,
) -> tuple[bytes, TaggedAnnotationRepairResult]:
    """Repair only annotation relationships derivable from existing PDF objects."""
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        pages_found = len(pdf.pages)
        pages = _page_map(pdf)
        skipped: list[str] = []
        unresolved: list[str] = []
        conflicts: list[str] = []
        actions: list[str] = []
        occurrences, annotations_found, supported_annotations = _annotation_entries(
            pdf,
            skipped=skipped,
        )
        root = pdf.Root.get("/StructTreeRoot")
        if not isinstance(root, pikepdf.Dictionary):
            unresolved.append("document has no valid /StructTreeRoot")
            return pdf_bytes, _result(
                pages_found=pages_found,
                annotations_found=annotations_found,
                supported_annotations=supported_annotations,
                skipped=skipped,
                unresolved=unresolved,
            )
        if str(root.get("/Type")) != "/StructTreeRoot":
            conflicts.append("document /StructTreeRoot has an invalid /Type")
        root_kids = root.get("/K")
        if root_kids is None:
            conflicts.append("StructTreeRoot is missing required /K")
        elif not isinstance(root_kids, (pikepdf.Array, pikepdf.Dictionary)):
            conflicts.append("StructTreeRoot /K is malformed")
        parent_tree = root.get("/ParentTree")
        if not isinstance(parent_tree, pikepdf.Dictionary):
            unresolved.append("document has no valid /ParentTree")
            return pdf_bytes, _result(
                pages_found=pages_found,
                annotations_found=annotations_found,
                supported_annotations=supported_annotations,
                skipped=skipped,
                unresolved=unresolved,
                conflicts=conflicts,
            )
        parent_pairs, parent_values, parent_issues = _scan_parent_tree(parent_tree)
        conflicts.extend(parent_issues)
        next_key_value = root.get("/ParentTreeNextKey")
        if next_key_value is not None and (
            _as_int(next_key_value) is None or int(next_key_value) < 0
        ):
            conflicts.append("StructTreeRoot /ParentTreeNextKey is invalid")
        reconnected = _reconnect_orphaned_parenttree_subtrees(
            pdf, root, parent_tree, actions
        )
        elements, objr_by_annotation, element_pages = _scan_structure(
            root,
            pages=pages,
            occurrences=occurrences,
            conflicts=conflicts,
        )
        element_keys = {_object_key(element) for element in elements}
        for key, value in parent_values.items():
            if isinstance(value, pikepdf.Dictionary) and (
                not _is_struct_elem(value) or _object_key(value) not in element_keys
            ):
                conflicts.append(
                    f"ParentTree key {key} points to a structure element "
                    "outside the structure tree"
                )

        for annotation_key, annotation_occurrences in occurrences.items():
            if (
                len(
                    {
                        _object_key(occurrence.page.obj)
                        for occurrence in annotation_occurrences
                    }
                )
                > 1
            ):
                conflicts.append(
                    "one annotation dictionary is shared by multiple pages; "
                    "relationship is ambiguous"
                )
            references = objr_by_annotation.get(annotation_key, [])
            if len({_object_key(reference.owner) for reference in references}) > 1:
                conflicts.append(
                    "one annotation is referenced by multiple structure-element owners"
                )
            if len({_object_key(reference.objr) for reference in references}) > 1:
                conflicts.append(
                    "one annotation has multiple structure /OBJR references"
                )
            for occurrence in annotation_occurrences:
                struct_parent = _as_int(occurrence.annotation.get("/StructParent"))
                if (
                    struct_parent is None
                    and occurrence.annotation.get("/StructParent") is not None
                ):
                    continue
                owner = (
                    parent_values.get(struct_parent)
                    if struct_parent is not None
                    else None
                )
                invalid_parent_owner = (
                    not isinstance(owner, pikepdf.Dictionary)
                    or not _is_struct_elem(owner)
                    or _object_key(owner) not in element_keys
                )
                if (
                    struct_parent is not None
                    and struct_parent in parent_values
                    and invalid_parent_owner
                ):
                    conflicts.append(
                        f"page {occurrence.page_number}: /StructParent {struct_parent} "
                        "does not resolve to a structure element"
                    )
                if (
                    struct_parent is not None
                    and struct_parent in parent_values
                    and references
                    and any(
                        not _same_object(owner, reference.owner)
                        for reference in references
                    )
                ):
                    conflicts.append(
                        f"page {occurrence.page_number}: /StructParent owner "
                        "conflicts with structure /OBJR owner"
                    )

        if conflicts:
            return pdf_bytes, _result(
                pages_found=pages_found,
                annotations_found=annotations_found,
                supported_annotations=supported_annotations,
                skipped=skipped,
                unresolved=unresolved,
                conflicts=conflicts,
            )

        used_keys = set(parent_values)
        for occurrence_list in occurrences.values():
            for occurrence in occurrence_list:
                value = _as_int(occurrence.annotation.get("/StructParent"))
                if value is not None:
                    used_keys.add(value)
        current_next_key = _as_int(next_key_value)
        next_key = max(
            (max(used_keys) + 1) if used_keys else 0,
            current_next_key or 0,
        )
        parent_additions: list[tuple[int, object]] = []
        objr_additions: list[
            tuple[pikepdf.Dictionary, pikepdf.Page, pikepdf.Dictionary]
        ] = []
        generated_elements: list[
            tuple[pikepdf.Page, pikepdf.Dictionary, pikepdf.Dictionary, int]
        ] = []
        annotation_changes: set[tuple[int, int] | int] = set()

        def allocate_key() -> int:
            nonlocal next_key
            while next_key in used_keys:
                next_key += 1
            allocated = next_key
            used_keys.add(allocated)
            next_key += 1
            return allocated

        for annotation_key, annotation_occurrences in occurrences.items():
            references = objr_by_annotation.get(annotation_key, [])
            for occurrence in annotation_occurrences:
                annotation = occurrence.annotation
                struct_parent = _as_int(annotation.get("/StructParent"))
                owner = (
                    parent_values.get(struct_parent)
                    if struct_parent is not None
                    else None
                )
                if references:
                    reference = references[0]
                    owner = reference.owner
                    if struct_parent is None:
                        struct_parent = allocate_key()
                        annotation["/StructParent"] = struct_parent
                        parent_additions.append((struct_parent, owner))
                        annotation_changes.add(annotation_key)
                        actions.append(
                            f"page {occurrence.page_number}: set annotation /StructParent "
                            f"to {struct_parent}"
                        )
                    elif struct_parent not in parent_values:
                        parent_additions.append((struct_parent, owner))
                        parent_values[struct_parent] = owner
                        actions.append(
                            f"page {occurrence.page_number}: restored ParentTree "
                            f"mapping {struct_parent} to existing /OBJR owner"
                        )
                    continue
                if struct_parent is not None and isinstance(owner, pikepdf.Dictionary):
                    owner_page = element_pages.get(_object_key(owner))
                    if owner_page is None:
                        unresolved.append(
                            f"page {occurrence.page_number}: ParentTree owner for "
                            f"/StructParent {struct_parent} has no matching /Pg"
                        )
                        continue
                    if not _same_object(owner_page.obj, occurrence.page.obj):
                        unresolved.append(
                            f"page {occurrence.page_number}: ParentTree owner for "
                            f"/StructParent {struct_parent} resolves to another page"
                        )
                        continue
                    objr_additions.append((owner, occurrence.page, annotation))
                    annotation_changes.add(annotation_key)
                    actions.append(
                        f"page {occurrence.page_number}: restored missing /OBJR for "
                        f"/StructParent {struct_parent}"
                    )
                    continue
                if struct_parent is not None:
                    unresolved.append(
                        f"page {occurrence.page_number}: /StructParent {struct_parent} "
                        "has no ParentTree owner"
                    )
                    continue
                key = allocate_key()
                objr = pdf.make_indirect(
                    pikepdf.Dictionary(
                        {
                            "/Type": pikepdf.Name("/OBJR"),
                            "/Obj": annotation,
                            "/Pg": occurrence.page.obj,
                        }
                    )
                )
                element = pdf.make_indirect(
                    pikepdf.Dictionary(
                        {
                            "/Type": pikepdf.Name("/StructElem"),
                            "/S": pikepdf.Name(
                                _annotation_role(
                                    _annotation_subtype(annotation) or "/Annot"
                                )
                            ),
                            "/Pg": occurrence.page.obj,
                            "/P": root,
                            "/K": objr,
                        }
                    )
                )
                generated_elements.append((occurrence.page, element, objr, key))
                annotation["/StructParent"] = key
                annotation_changes.add(annotation_key)
                parent_additions.append((key, element))
                actions.append(
                    f"page {occurrence.page_number}: created tagged annotation "
                    f"structure element with /StructParent {key}"
                )

        for owner, page, annotation in objr_additions:
            objr = pdf.make_indirect(
                pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/OBJR"),
                        "/Obj": annotation,
                        "/Pg": page.obj,
                    }
                )
            )
            current_kids = owner.get("/K")
            if isinstance(current_kids, pikepdf.Array):
                current_kids.append(objr)
            elif current_kids is None:
                owner["/K"] = objr
            else:
                owner["/K"] = pikepdf.Array([current_kids, objr])

        root_kids = root.get("/K")
        for _page, element, _objr, _key in generated_elements:
            if isinstance(root_kids, pikepdf.Array):
                root_kids.append(element)
            elif root_kids is None:
                root_kids = pikepdf.Array([element])
                root["/K"] = root_kids
            else:
                root_kids = pikepdf.Array([root_kids, element])
                root["/K"] = root_kids

        if parent_additions:
            _write_parent_tree_nums(
                parent_tree,
                parent_pairs + parent_additions,
            )
            for key, value in parent_additions:
                parent_values[key] = value

        required_next_key = max(
            [key + 1 for key, _value in _flatten_parent_tree(parent_tree)]
            + [next_key, current_next_key or 0, 0]
        )
        parent_tree_next_key_updated = (
            current_next_key is not None and current_next_key < required_next_key
        ) or (bool(parent_additions) and current_next_key is None)
        if parent_tree_next_key_updated:
            root["/ParentTreeNextKey"] = required_next_key
            actions.append(
                f"StructTreeRoot: set /ParentTreeNextKey to {required_next_key}"
            )

        changed = bool(
            annotation_changes
            or objr_additions
            or generated_elements
            or parent_additions
            or reconnected
        )
        if not changed and not parent_tree_next_key_updated:
            return pdf_bytes, _result(
                pages_found=pages_found,
                annotations_found=annotations_found,
                supported_annotations=supported_annotations,
                skipped=skipped,
                unresolved=unresolved,
                conflicts=conflicts,
            )

        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue(), _result(
            pages_found=pages_found,
            annotations_found=annotations_found,
            supported_annotations=supported_annotations,
            skipped=skipped,
            unresolved=unresolved,
            conflicts=conflicts,
            actions=actions,
            annotations_repaired=len(annotation_changes),
            struct_elements_created=len(generated_elements),
            objr_created=len(generated_elements) + len(objr_additions),
            parent_tree_entries_added=len(parent_additions),
            parent_tree_entries_updated=len(parent_additions),
            parent_tree_next_key_updated=parent_tree_next_key_updated,
        )
