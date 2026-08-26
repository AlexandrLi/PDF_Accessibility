"""Tests for safe tagged-content relationship repairs."""

from __future__ import annotations

import io
import unittest

import pikepdf

from lib.tagged_content_sweep import repair_tagged_content


def _make_pdf(
    *,
    content_mcids: tuple[int, ...] = (0,),
    struct_mcid: int = 0,
    struct_parents: int | None = None,
    parent_tree_array: pikepdf.Array | None = None,
    include_child_parent: bool = False,
    use_mcr_dictionary: bool = False,
) -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    content = b" ".join(
        f"/Span << /MCID {mcid} >> BDC EMC".encode("ascii")
        for mcid in content_mcids
    )
    page["/Contents"] = pdf.make_stream(content)
    if struct_parents is not None:
        page["/StructParents"] = struct_parents

    child = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/P"),
                "/Pg": page.obj,
                "/K": struct_mcid,
            }
        )
    )
    if use_mcr_dictionary:
        child["/K"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/MCR"),
                "/Pg": page.obj,
                "/MCID": struct_mcid,
            }
        )
    if include_child_parent:
        child["/P"] = None
    root = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([child]),
            }
        )
    )
    if parent_tree_array is not None:
        parent_tree = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Nums": pikepdf.Array(
                        [struct_parents if struct_parents is not None else 0, parent_tree_array]
                    )
                }
            )
        )
        root["/ParentTree"] = parent_tree
    pdf.Root["/StructTreeRoot"] = root
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


class TaggedContentSweepTests(unittest.TestCase):
    def test_repairs_missing_parent_page_and_parent_tree_relationships(self) -> None:
        repaired, result = repair_tagged_content(_make_pdf())

        self.assertEqual(result.pages_found, 1)
        self.assertEqual(result.struct_elements_found, 1)
        self.assertEqual(result.pages_updated, 1)
        self.assertEqual(result.struct_elements_updated, 1)
        self.assertEqual(result.parent_tree_entries_added, 1)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            page = pdf.pages[0]
            root = pdf.Root["/StructTreeRoot"]
            child = root["/K"][0]
            self.assertEqual(child["/P"], root)
            self.assertIsNotNone(page.get("/StructParents"))
            parent_tree = root["/ParentTree"]
            key = int(page["/StructParents"])
            self.assertEqual(parent_tree["/Nums"][0], key)
            self.assertEqual(parent_tree["/Nums"][1][0], child)
            self.assertGreaterEqual(
                int(root["/ParentTreeNextKey"]),
                key + 1,
            )

    def test_preserves_existing_parent_tree_mapping(self) -> None:
        original = _make_pdf(struct_parents=4)
        with pikepdf.open(io.BytesIO(original)) as pdf:
            root = pdf.Root["/StructTreeRoot"]
            child = root["/K"][0]
            root["/ParentTree"] = pdf.make_indirect(
                pikepdf.Dictionary(
                    {"/Nums": pikepdf.Array([4, pikepdf.Array([child])])}
                )
            )
            saved = io.BytesIO()
            pdf.save(saved)
            original = saved.getvalue()
        repaired, result = repair_tagged_content(original)

        self.assertEqual(result.parent_tree_entries_added, 0)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            mapping = pdf.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"][1]
            self.assertEqual(mapping[0].objgen, pdf.Root["/StructTreeRoot"]["/K"][0].objgen)

    def test_reports_conflicting_parent_tree_mapping(self) -> None:
        existing = pikepdf.Dictionary({"/Type": pikepdf.Name("/StructElem")})
        repaired, result = repair_tagged_content(
            _make_pdf(
                struct_parents=4,
                parent_tree_array=pikepdf.Array([existing]),
            )
        )

        self.assertTrue(result.conflicts)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            mapping = pdf.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"][1]
            self.assertEqual(mapping[0]["/Type"], pikepdf.Name("/StructElem"))

    def test_reports_orphan_mcid_without_creating_a_struct_element(self) -> None:
        original = _make_pdf(content_mcids=(0, 7))
        repaired, result = repair_tagged_content(original)

        self.assertIn("page 1 MCID 7", result.unresolved_mcids)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(len(pdf.pages), 1)
            self.assertEqual(len(pdf.Root["/StructTreeRoot"]["/K"]), 1)
            parent_tree = pdf.Root["/StructTreeRoot"]["/ParentTree"]
            self.assertEqual(len(parent_tree["/Nums"][1]), 1)

    def test_repairs_mcr_dictionary_linkage(self) -> None:
        repaired, result = repair_tagged_content(_make_pdf(use_mcr_dictionary=True))

        self.assertEqual(result.mapped_mcids, 1)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            root = pdf.Root["/StructTreeRoot"]
            child = root["/K"][0]
            page_key = int(pdf.pages[0]["/StructParents"])
            self.assertEqual(root["/ParentTree"]["/Nums"][1][0].objgen, child.objgen)
            self.assertEqual(page_key, 0)

    def test_second_run_is_idempotent_and_output_remains_valid(self) -> None:
        repaired_once, _first = repair_tagged_content(_make_pdf())
        repaired_twice, second = repair_tagged_content(repaired_once)

        self.assertEqual(second.pages_updated, 0)
        self.assertEqual(second.struct_elements_updated, 0)
        self.assertEqual(second.parent_tree_entries_added, 0)
        self.assertEqual(second.actions, [])
        self.assertEqual(repaired_twice, repaired_once)
        with pikepdf.open(io.BytesIO(repaired_twice)) as pdf:
            self.assertEqual(len(pdf.pages), 1)


if __name__ == "__main__":
    unittest.main()
