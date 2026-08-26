"""Tests for conservative tagged-PDF annotation relationship repair."""

from __future__ import annotations

import io
import unittest

import pikepdf

from lib.tagged_annotation_sweep import repair_tagged_annotations


def _save(pdf: pikepdf.Pdf) -> bytes:
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


def _base_pdf(
    page_count: int = 1,
) -> tuple[pikepdf.Pdf, list[pikepdf.Page], pikepdf.Dictionary]:
    pdf = pikepdf.new()
    pages = [pdf.add_blank_page(page_size=(200, 200)) for _ in range(page_count)]
    root = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array(),
                "/ParentTree": pdf.make_indirect(
                    pikepdf.Dictionary({"/Nums": pikepdf.Array()})
                ),
            }
        )
    )
    pdf.Root["/StructTreeRoot"] = root
    return pdf, pages, root


def _annotation(
    pdf: pikepdf.Pdf,
    subtype: str,
    *,
    contents: str = "keep this",
) -> pikepdf.Dictionary:
    return pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Annot"),
                "/Subtype": pikepdf.Name(subtype),
                "/Rect": pikepdf.Array([10, 20, 50, 60]),
                "/Contents": pikepdf.String(contents),
                "/A": pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/Action"),
                        "/S": pikepdf.Name("/URI"),
                        "/URI": pikepdf.String("https://example.test"),
                    }
                ),
            }
        )
    )


def _objr(
    pdf: pikepdf.Pdf,
    annotation: pikepdf.Dictionary,
    page: pikepdf.Page,
) -> pikepdf.Dictionary:
    return pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/OBJR"),
                "/Obj": annotation,
                "/Pg": page.obj,
            }
        )
    )


def _owner(
    pdf: pikepdf.Pdf,
    root: pikepdf.Dictionary,
    page: pikepdf.Page,
    *,
    role: str = "/Annot",
    annotation: pikepdf.Dictionary | None = None,
) -> pikepdf.Dictionary:
    data: dict[str, object] = {
        "/Type": pikepdf.Name("/StructElem"),
        "/S": pikepdf.Name(role),
        "/Pg": page.obj,
        "/P": root,
    }
    if annotation is not None:
        data["/K"] = _objr(pdf, annotation, page)
    owner = pdf.make_indirect(pikepdf.Dictionary(data))
    root["/K"].append(owner)
    return owner


def _set_parent_tree(
    root: pikepdf.Dictionary,
    pairs: list[tuple[int, object]],
    *,
    use_kids: bool = False,
) -> None:
    parent_tree = root["/ParentTree"]
    nums = pikepdf.Array()
    for key, value in pairs:
        nums.extend((key, value))
    if use_kids:
        del parent_tree["/Nums"]
        parent_tree["/Kids"] = pikepdf.Array(
            [
                pikepdf.Dictionary(
                    {
                        "/Nums": nums,
                        "/Limits": pikepdf.Array([pairs[0][0], pairs[-1][0]]),
                    }
                )
            ]
        )
    else:
        parent_tree["/Nums"] = nums


def _page_annotation(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    subtype: str,
    *,
    contents: str = "keep this",
) -> pikepdf.Dictionary:
    annotation = _annotation(pdf, subtype, contents=contents)
    page["/Annots"] = pikepdf.Array([annotation])
    return annotation


class TaggedAnnotationSweepTests(unittest.TestCase):
    def test_creates_minimal_link_widget_and_generic_elements(self) -> None:
        pdf, pages, root = _base_pdf()
        annotations = [
            _page_annotation(pdf, pages[0], "/Link"),
            _annotation(pdf, "/Widget"),
            _annotation(pdf, "/Text"),
        ]
        pages[0]["/Annots"].extend(annotations[1:])

        repaired, result = repair_tagged_annotations(_save(pdf))

        self.assertEqual(result.annotations_found, 3)
        self.assertEqual(result.struct_elements_created, 3)
        self.assertEqual(result.objr_created, 3)
        with pikepdf.open(io.BytesIO(repaired)) as checked:
            root = checked.Root["/StructTreeRoot"]
            self.assertEqual(
                [str(element["/S"]) for element in root["/K"]],
                ["/Link", "/Form", "/Annot"],
            )
            self.assertEqual(
                [
                    int(annotation["/StructParent"])
                    for annotation in checked.pages[0]["/Annots"]
                ],
                [0, 1, 2],
            )
            self.assertEqual(
                [int(value["/S"] == "/Link") for value in root["/K"]],
                [1, 0, 0],
            )
            for element in root["/K"]:
                self.assertEqual(element["/P"], root)
                self.assertEqual(element["/K"]["/Type"], pikepdf.Name("/OBJR"))
                self.assertEqual(element["/K"]["/Pg"], checked.pages[0].obj)

    def test_preserves_already_valid_linkage(self) -> None:
        pdf, pages, root = _base_pdf()
        annotation = _page_annotation(pdf, pages[0], "/Link")
        annotation["/StructParent"] = 4
        owner = _owner(pdf, root, pages[0], role="/Link", annotation=annotation)
        _set_parent_tree(root, [(4, owner)])
        original = _save(pdf)

        repaired, result = repair_tagged_annotations(original)

        self.assertEqual(repaired, original)
        self.assertEqual(result.actions, [])
        self.assertEqual(result.annotations_repaired, 0)

    def test_repairs_missing_parent_tree_mapping_from_objr(self) -> None:
        pdf, pages, root = _base_pdf()
        annotation = _page_annotation(pdf, pages[0], "/Link")
        annotation["/StructParent"] = 7
        _owner(pdf, root, pages[0], role="/Link", annotation=annotation)

        repaired, result = repair_tagged_annotations(_save(pdf))

        self.assertEqual(result.parent_tree_entries_added, 1)
        with pikepdf.open(io.BytesIO(repaired)) as checked:
            values = checked.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"]
            self.assertEqual(values[0], 7)
            self.assertEqual(
                values[1].objgen, checked.Root["/StructTreeRoot"]["/K"][0].objgen
            )

    def test_repairs_missing_struct_parent_from_objr(self) -> None:
        pdf, pages, root = _base_pdf()
        annotation = _page_annotation(pdf, pages[0], "/Widget")
        _owner(pdf, root, pages[0], role="/Form", annotation=annotation)

        repaired, result = repair_tagged_annotations(_save(pdf))

        self.assertEqual(result.annotations_repaired, 1)
        self.assertEqual(result.parent_tree_entries_added, 1)
        with pikepdf.open(io.BytesIO(repaired)) as checked:
            annotation = checked.pages[0]["/Annots"][0]
            key = int(annotation["/StructParent"])
            values = checked.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"]
            owner_index = values.index(key) + 1
            self.assertEqual(
                values[owner_index].objgen,
                checked.Root["/StructTreeRoot"]["/K"][0].objgen,
            )

    def test_repairs_missing_objr_from_unambiguous_parent_tree_owner(self) -> None:
        pdf, pages, root = _base_pdf()
        annotation = _page_annotation(pdf, pages[0], "/Text")
        annotation["/StructParent"] = 5
        owner = _owner(pdf, root, pages[0], role="/Annot")
        _set_parent_tree(root, [(5, owner)])

        repaired, result = repair_tagged_annotations(_save(pdf))

        self.assertEqual(result.objr_created, 1)
        with pikepdf.open(io.BytesIO(repaired)) as checked:
            child = checked.Root["/StructTreeRoot"]["/K"][0]["/K"]
            self.assertEqual(
                child["/Obj"].objgen, checked.pages[0]["/Annots"][0].objgen
            )
            self.assertEqual(child["/Pg"], checked.pages[0].obj)

    def test_repairs_missing_objr_when_owner_inherits_page(self) -> None:
        pdf, pages, root = _base_pdf()
        annotation = _page_annotation(pdf, pages[0], "/Link")
        annotation["/StructParent"] = 6
        owner = _owner(pdf, root, pages[0], role="/Link")
        del owner["/Pg"]
        section = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/Sect"),
                    "/Pg": pages[0].obj,
                    "/P": root,
                    "/K": owner,
                }
            )
        )
        owner["/P"] = section
        root["/K"] = pikepdf.Array([section])
        _set_parent_tree(root, [(6, owner)])

        repaired, result = repair_tagged_annotations(_save(pdf))

        self.assertEqual(result.objr_created, 1)
        self.assertEqual(result.unresolved, [])
        with pikepdf.open(io.BytesIO(repaired)) as checked:
            owner = checked.Root["/StructTreeRoot"]["/K"][0]["/K"]
            self.assertEqual(owner["/K"]["/Obj"], checked.pages[0]["/Annots"][0])
            self.assertEqual(owner["/K"]["/Pg"], checked.pages[0].obj)

    def test_conflicting_relationships_are_unchanged(self) -> None:
        pdf, pages, root = _base_pdf()
        annotation = _page_annotation(pdf, pages[0], "/Link")
        annotation["/StructParent"] = 3
        objr_owner = _owner(pdf, root, pages[0], role="/Link", annotation=annotation)
        other_owner = _owner(pdf, root, pages[0], role="/Annot")
        _set_parent_tree(root, [(3, other_owner)])
        original = _save(pdf)

        repaired, result = repair_tagged_annotations(original)

        self.assertEqual(repaired, original)
        self.assertTrue(result.conflicts)
        self.assertEqual(objr_owner["/K"]["/Obj"].objgen, annotation.objgen)

    def test_skips_unsupported_and_malformed_annotations(self) -> None:
        pdf, pages, _root = _base_pdf()
        popup = _annotation(pdf, "/Popup")
        malformed = pikepdf.String("not an annotation")
        pages[0]["/Annots"] = pikepdf.Array([popup, malformed])
        original = _save(pdf)

        repaired, result = repair_tagged_annotations(original)

        self.assertEqual(repaired, original)
        self.assertEqual(result.struct_elements_created, 0)
        self.assertEqual(len(result.skipped), 2)

    def test_malformed_parent_tree_is_unchanged(self) -> None:
        pdf, pages, root = _base_pdf()
        _page_annotation(pdf, pages[0], "/Link")
        root["/ParentTree"]["/Kids"] = pikepdf.String("bad kids")
        original = _save(pdf)

        repaired, result = repair_tagged_annotations(original)

        self.assertEqual(repaired, original)
        self.assertTrue(result.conflicts)
        self.assertEqual(result.struct_elements_created, 0)

    def test_handles_parent_tree_kids_pages_and_collision_free_keys(self) -> None:
        pdf, pages, root = _base_pdf(page_count=2)
        first = _page_annotation(pdf, pages[0], "/Link", contents="first")
        second = _page_annotation(pdf, pages[1], "/Text", contents="second")
        first["/StructParent"] = 10
        existing_owner = _owner(pdf, root, pages[0], role="/Link", annotation=first)
        _set_parent_tree(root, [(10, existing_owner)], use_kids=True)
        root["/ParentTreeNextKey"] = 3

        repaired, result = repair_tagged_annotations(_save(pdf))

        self.assertEqual(result.struct_elements_created, 1)
        with pikepdf.open(io.BytesIO(repaired)) as checked:
            second = checked.pages[1]["/Annots"][0]
            self.assertEqual(int(second["/StructParent"]), 11)
            self.assertEqual(
                int(checked.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"][0]),
                10,
            )
            self.assertEqual(
                int(checked.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"][2]), 11
            )
            self.assertEqual(
                int(checked.Root["/StructTreeRoot"]["/ParentTreeNextKey"]), 12
            )
            self.assertEqual(len(checked.pages), 2)

    def test_preserves_annotation_data_and_is_idempotent(self) -> None:
        pdf, pages, _root = _base_pdf()
        annotation = _page_annotation(pdf, pages[0], "/Link", contents="preserve")
        original_rect = list(annotation["/Rect"])
        original_action = annotation["/A"]["/URI"]
        original_content = pages[0].objgen

        repaired_once, _first = repair_tagged_annotations(_save(pdf))
        repaired_twice, second = repair_tagged_annotations(repaired_once)

        self.assertEqual(repaired_twice, repaired_once)
        self.assertEqual(second.actions, [])
        with pikepdf.open(io.BytesIO(repaired_twice)) as checked:
            annotation = checked.pages[0]["/Annots"][0]
            self.assertEqual(list(annotation["/Rect"]), original_rect)
            self.assertEqual(annotation["/A"]["/URI"], original_action)
            self.assertEqual(checked.pages[0].objgen, original_content)

    def test_missing_root_or_parent_tree_is_reported_without_changes(self) -> None:
        pdf = pikepdf.new()
        page = pdf.add_blank_page(page_size=(200, 200))
        page["/Annots"] = pikepdf.Array([_annotation(pdf, "/Link")])
        original = _save(pdf)
        repaired, result = repair_tagged_annotations(original)
        self.assertEqual(repaired, original)
        self.assertTrue(result.unresolved)

        pdf, page_list, root = _base_pdf()
        page_list[0]["/Annots"] = pikepdf.Array([_annotation(pdf, "/Link")])
        del root["/ParentTree"]
        original = _save(pdf)
        repaired, result = repair_tagged_annotations(original)
        self.assertEqual(repaired, original)
        self.assertTrue(result.unresolved)


if __name__ == "__main__":
    unittest.main()
