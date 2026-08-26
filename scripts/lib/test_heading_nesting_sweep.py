"""Tests for conservative heading-nesting repair."""

from __future__ import annotations

import io
import unittest

import pikepdf

from lib.heading_nesting_sweep import repair_heading_nesting


def _make_pdf(
    roles: list[str],
    *,
    nested_roles: list[str] | None = None,
    role_map: dict[str, str] | None = None,
    with_structure_tree: bool = True,
) -> bytes:
    pdf = pikepdf.new()
    pages = [
        pdf.add_blank_page(page_size=(200, 200)),
        pdf.add_blank_page(page_size=(200, 200)),
    ]
    for page_number, page in enumerate(pages, start=1):
        page["/Contents"] = pdf.make_stream(
            f"BT /F1 12 Tf (page {page_number} text) Tj ET".encode("ascii")
        )

    elements = [
        pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name(role),
                    "/Pg": pages[index % len(pages)].obj,
                    "/T": pikepdf.String(f"heading {index}"),
                }
            )
        )
        for index, role in enumerate(roles)
    ]
    if nested_roles:
        nested = [
            pdf.make_indirect(
                pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name(role),
                        "/Pg": pages[0].obj,
                        "/T": pikepdf.String(f"nested heading {index}"),
                    }
                )
            )
            for index, role in enumerate(nested_roles)
        ]
        elements[0]["/K"] = pikepdf.Array(nested)

    if with_structure_tree:
        root_data = {
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array(elements),
        }
        if role_map:
            mapped_roles = pikepdf.Dictionary()
            for role, target in role_map.items():
                mapped_roles[role] = pikepdf.Name(target)
            root_data["/RoleMap"] = mapped_roles
        pdf.Root["/StructTreeRoot"] = pdf.make_indirect(
            pikepdf.Dictionary(root_data)
        )

    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


def _roles(pdf_bytes: bytes) -> list[str]:
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        root = pdf.Root["/StructTreeRoot"]
        return [str(element["/S"]) for element in root["/K"]]


class HeadingNestingSweepTests(unittest.TestCase):
    def test_valid_sequence_is_byte_identical(self) -> None:
        original = _make_pdf(["/H1", "/H2", "/H2", "/H3", "/H1"])

        repaired, result = repair_heading_nesting(original)

        self.assertEqual(repaired, original)
        self.assertEqual(result.headings_updated, 0)
        self.assertEqual(result.actions, [])

    def test_first_heading_is_normalized_to_h1(self) -> None:
        repaired, result = repair_heading_nesting(
            _make_pdf(["/H3", "/H4", "/H6"])
        )

        self.assertEqual(_roles(repaired), ["/H1", "/H2", "/H3"])
        self.assertEqual(result.headings_updated, 3)
        self.assertEqual(len(result.changed_roles), 3)

    def test_jumps_are_lowered_against_previous_normalized_level(self) -> None:
        repaired, _result = repair_heading_nesting(
            _make_pdf(["/H1", "/H3", "/H6", "/H2", "/H5"])
        )

        self.assertEqual(_roles(repaired), ["/H1", "/H2", "/H3", "/H2", "/H3"])

    def test_decreases_and_repeated_levels_are_preserved(self) -> None:
        original = _make_pdf(["/H1", "/H3", "/H2", "/H2", "/H1", "/H4"])

        repaired, _result = repair_heading_nesting(original)

        self.assertEqual(_roles(repaired), ["/H1", "/H2", "/H2", "/H2", "/H1", "/H2"])

    def test_nested_elements_follow_structure_document_order(self) -> None:
        original = _make_pdf(
            ["/H3", "/H1"],
            nested_roles=["/H4", "/H6"],
        )

        repaired, result = repair_heading_nesting(original)

        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            root = pdf.Root["/StructTreeRoot"]
            self.assertEqual(str(root["/K"][0]["/S"]), "/H1")
            self.assertEqual(
                [str(child["/S"]) for child in root["/K"][0]["/K"]],
                ["/H2", "/H3"],
            )
            self.assertEqual(str(root["/K"][1]["/S"]), "/H1")
        self.assertEqual(result.headings_found, 4)

    def test_custom_and_generic_roles_are_preserved_and_reported(self) -> None:
        original = _make_pdf(
            ["/Heading", "/H", "/H4"],
            role_map={"/Heading": "/H2"},
        )

        repaired, result = repair_heading_nesting(original)

        self.assertEqual(repaired != original, True)
        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            roles = [str(element["/S"]) for element in pdf.Root["/StructTreeRoot"]["/K"]]
        self.assertEqual(roles, ["/Heading", "/H", "/H1"])
        self.assertTrue(any("custom role /Heading" in item for item in result.diagnostics))
        self.assertTrue(any("generic /H preserved" in item for item in result.diagnostics))
        self.assertTrue(result.unresolved)

    def test_standard_non_heading_roles_do_not_create_role_map_noise(self) -> None:
        original = _make_pdf(["/P", "/Sect", "/H1", "/H2"])

        repaired, result = repair_heading_nesting(original)

        self.assertEqual(repaired, original)
        self.assertEqual(result.unresolved, [])
        self.assertEqual(result.diagnostics, [])

    def test_preserves_pages_content_and_text(self) -> None:
        original = _make_pdf(["/H1", "/H4"])
        with pikepdf.open(io.BytesIO(original)) as pdf:
            original_content = [page["/Contents"].read_bytes() for page in pdf.pages]
            original_text = [
                str(element["/T"]) for element in pdf.Root["/StructTreeRoot"]["/K"]
            ]

        repaired, _result = repair_heading_nesting(original)

        with pikepdf.open(io.BytesIO(repaired)) as pdf:
            self.assertEqual(
                [page["/Contents"].read_bytes() for page in pdf.pages],
                original_content,
            )
            self.assertEqual(
                [str(element["/T"]) for element in pdf.Root["/StructTreeRoot"]["/K"]],
                original_text,
            )
            self.assertEqual(len(pdf.pages), 2)

    def test_no_structure_tree_is_nonblocking_and_unchanged(self) -> None:
        original = _make_pdf([], with_structure_tree=False)

        repaired, result = repair_heading_nesting(original)

        self.assertEqual(repaired, original)
        self.assertEqual(result.struct_elements_found, 0)
        self.assertTrue(result.unresolved)

    def test_second_run_is_idempotent(self) -> None:
        repaired_once, _first = repair_heading_nesting(_make_pdf(["/H2", "/H5"]))

        repaired_twice, second = repair_heading_nesting(repaired_once)

        self.assertEqual(repaired_twice, repaired_once)
        self.assertEqual(second.headings_updated, 0)
        self.assertEqual(second.actions, [])


if __name__ == "__main__":
    unittest.main()
