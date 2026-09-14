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


def _make_tagged_pdf(
    blocks: tuple[tuple[int, str, bool], ...],
    owned: tuple[int, ...],
    *,
    wrap_in_document: bool = True,
    group_type: str = "/Document",
) -> bytes:
    """A page of marked content under /Document, tagging only ``owned`` MCIDs.

    Each block is ``(mcid, tag, shows_text)``. A block that shows text draws a
    string; the others only set a graphics state, the way decoration does.
    """
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    body = []
    for mcid, tag, shows_text in blocks:
        drawing = (
            f"BT /F1 12 Tf 10 {180 - mcid * 20} Td (Line {mcid}) Tj ET"
            if shows_text
            else "q 1 0 0 RG Q"
        )
        body.append(f"/{tag} << /MCID {mcid} >> BDC {drawing} EMC")
    page["/Contents"] = pdf.make_stream(" ".join(body).encode("ascii"))
    page["/StructParents"] = 0

    kids = [
        pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/P"),
                    "/Pg": page.obj,
                    "/K": mcid,
                }
            )
        )
        for mcid in owned
    ]
    top = pikepdf.Array(kids)
    if wrap_in_document:
        document = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name(group_type),
                    "/K": top,
                }
            )
        )
        for kid in kids:
            kid["/P"] = document
        top = pikepdf.Array([document])
    root = pdf.make_indirect(
        pikepdf.Dictionary(
            {"/Type": pikepdf.Name("/StructTreeRoot"), "/K": top}
        )
    )
    root["/ParentTree"] = pdf.make_indirect(
        pikepdf.Dictionary({"/Nums": pikepdf.Array([0, pikepdf.Array(kids)])})
    )
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


    def test_adopts_an_orphan_text_block_at_the_position_its_mcid_gives_it(self) -> None:
        original = _make_tagged_pdf(
            blocks=((0, "P", True), (1, "P", True), (2, "P", True)),
            owned=(0, 2),
        )
        repaired, result = repair_tagged_content(original)

        self.assertEqual(result.struct_elements_added, 1)
        self.assertEqual(result.unresolved_mcids, [])
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            document = pdf.Root["/StructTreeRoot"]["/K"][0]
            kids = document["/K"]
            self.assertEqual([int(kid["/K"]) for kid in kids], [0, 1, 2])
            adopted = kids[1]
            self.assertEqual(adopted["/S"], pikepdf.Name("/P"))
            self.assertEqual(adopted["/P"].objgen, document.objgen)
            self.assertEqual(adopted["/Pg"].objgen, pdf.pages[0].obj.objgen)

    def test_leaves_an_orphan_block_that_shows_no_text_outside_the_tree(self) -> None:
        original = _make_tagged_pdf(
            blocks=((0, "P", True), (1, "P", False), (2, "P", True)),
            owned=(0, 2),
        )
        repaired, result = repair_tagged_content(original)

        self.assertEqual(result.struct_elements_added, 0)
        self.assertIn("page 1 MCID 1", result.unresolved_mcids)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            document = pdf.Root["/StructTreeRoot"]["/K"][0]
            self.assertEqual(len(document["/K"]), 2)

    def test_does_not_adopt_an_orphan_beside_the_structure_tree_root(self) -> None:
        original = _make_tagged_pdf(
            blocks=((0, "P", True), (1, "P", True)),
            owned=(0,),
            wrap_in_document=False,
        )
        repaired, result = repair_tagged_content(original)

        self.assertEqual(result.struct_elements_added, 0)
        self.assertIn("page 1 MCID 1", result.unresolved_mcids)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(len(pdf.Root["/StructTreeRoot"]["/K"]), 1)

    def test_remaps_a_parent_tree_entry_that_names_another_mcids_element(self) -> None:
        original = _make_tagged_pdf(
            blocks=((0, "P", True), (1, "P", True)),
            owned=(0, 1),
        )
        with pikepdf.open(io.BytesIO(original)) as pdf:
            document = pdf.Root["/StructTreeRoot"]["/K"][0]
            first, second = document["/K"][0], document["/K"][1]
            # The producer numbered a block it never tagged, so every entry
            # after the gap names the element belonging to the MCID before it.
            pdf.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"][1] = pikepdf.Array(
                [second, first]
            )
            shifted = io.BytesIO()
            pdf.save(shifted)
            original = shifted.getvalue()

        repaired, result = repair_tagged_content(original)

        self.assertEqual(result.parent_tree_entries_updated, 2)
        self.assertEqual(result.conflicts, [])
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            document = pdf.Root["/StructTreeRoot"]["/K"][0]
            mapping = pdf.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"][1]
            for mcid, entry in enumerate(mapping):
                self.assertEqual(entry.objgen, document["/K"][mcid].objgen)

    def test_does_not_adopt_an_orphan_into_a_list_or_table_container(self) -> None:
        for group_type in ("/L", "/Table", "/TR"):
            with self.subTest(group_type=group_type):
                repaired, result = repair_tagged_content(
                    _make_tagged_pdf(
                        blocks=((0, "P", True), (1, "P", True), (2, "P", True)),
                        owned=(0, 2),
                        group_type=group_type,
                    )
                )

                self.assertEqual(result.struct_elements_added, 0)
                self.assertIn("page 1 MCID 1", result.unresolved_mcids)
                with pikepdf.open(io.BytesIO(repaired)) as pdf:
                    group = pdf.Root["/StructTreeRoot"]["/K"][0]
                    self.assertEqual(len(group["/K"]), 2)

    def test_does_not_adopt_an_orphan_block_that_shows_only_whitespace(self) -> None:
        original = _make_tagged_pdf(
            blocks=((0, "P", True), (2, "P", True)),
            owned=(0, 2),
        )
        with pikepdf.open(io.BytesIO(original)) as pdf:
            page = pdf.pages[0]
            page["/Contents"] = pdf.make_stream(
                bytes(page["/Contents"].read_bytes())
                + b" /P << /MCID 1 >> BDC BT /F1 12 Tf 10 150 Td ( ) Tj ET EMC"
            )
            spaced = io.BytesIO()
            pdf.save(spaced)
            original = spaced.getvalue()

        repaired, result = repair_tagged_content(original)

        self.assertEqual(result.struct_elements_added, 0)
        self.assertIn("page 1 MCID 1", result.unresolved_mcids)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            document = pdf.Root["/StructTreeRoot"]["/K"][0]
            self.assertEqual(len(document["/K"]), 2)

if __name__ == "__main__":
    unittest.main()
