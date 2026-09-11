"""Focused tests for conservative table audit telemetry."""

from __future__ import annotations

import io
import json
import unittest

import pikepdf

from lib.pdf_a11y_audit import audit_pdf_bytes


def _make_table_pdf(*, residual: bool) -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    if residual:
        header_one = pikepdf.Dictionary(
            {
                "/S": pikepdf.Name("/TH"),
                "/ID": pikepdf.String("duplicate"),
                "/Scope": pikepdf.Name("/Column"),
            }
        )
        header_two = pikepdf.Dictionary(
            {
                "/S": pikepdf.Name("/TH"),
                "/ID": pikepdf.String("duplicate"),
                "/Scope": pikepdf.Name("/Sideways"),
            }
        )
        rows = [
            pikepdf.Dictionary(
                {
                    "/S": pikepdf.Name("/TR"),
                    "/K": pikepdf.Array(
                        [
                            header_one,
                            header_two,
                        ]
                    ),
                }
            ),
            pikepdf.Dictionary(
                {
                    "/S": pikepdf.Name("/TR"),
                    "/K": pikepdf.Array(
                        [
                            pikepdf.Dictionary(
                                {"/S": pikepdf.Name("/TD"), "/Pg": page.obj}
                            ),
                            pikepdf.Dictionary(
                                {
                                    "/S": pikepdf.Name("/TD"),
                                    "/Headers": pikepdf.String("not-an-array"),
                                }
                            ),
                            pikepdf.Dictionary(
                                {
                                    "/S": pikepdf.Name("/TD"),
                                    "/Headers": pikepdf.Array(
                                        [pikepdf.String("missing-header")]
                                    ),
                                }
                            ),
                        ]
                    ),
                }
            ),
            pikepdf.Dictionary(
                {
                    "/S": pikepdf.Name("/TR"),
                    "/K": pikepdf.Array(
                        [
                            pikepdf.Dictionary({"/S": pikepdf.Name("/TD")}),
                            pikepdf.Dictionary({"/S": pikepdf.Name("/TD")}),
                        ]
                    ),
                }
            ),
            pikepdf.Dictionary({"/S": pikepdf.Name("/Div")}),
        ]
    else:
        header = pikepdf.Dictionary(
            {
                "/S": pikepdf.Name("/TH"),
                "/ID": pikepdf.String("header"),
                "/Scope": pikepdf.Name("/Column"),
            }
        )
        rows = [
            pikepdf.Dictionary(
                {"/S": pikepdf.Name("/TR"), "/K": pikepdf.Array([header])}
            ),
            pikepdf.Dictionary(
                {
                    "/S": pikepdf.Name("/TR"),
                    "/K": pikepdf.Array(
                        [
                            pikepdf.Dictionary(
                                {
                                    "/S": pikepdf.Name("/TD"),
                                    "/Headers": pikepdf.Array(
                                        [pikepdf.String("header")]
                                    ),
                                }
                            )
                        ]
                    ),
                }
            ),
        ]
    table = pikepdf.Dictionary(
        {
            "/S": pikepdf.Name("/Table"),
            "/Summary": pikepdf.String("Example"),
            "/K": pikepdf.Array(rows),
        }
    )
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
    pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
        {"/K": pikepdf.Array([table])}
    )
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


class PdfA11yAuditTests(unittest.TestCase):
    def test_clean_table_has_no_table_residuals_and_does_not_block(self) -> None:
        audit = audit_pdf_bytes(_make_table_pdf(residual=False))

        self.assertEqual(audit.table_count, 1)
        self.assertEqual(audit.tables_without_th, 0)
        self.assertEqual(audit.tables_th_missing_scope, [])
        self.assertEqual(audit.tables_th_duplicate_id, [])
        self.assertEqual(audit.data_cells_missing_headers, [])
        self.assertFalse(audit.has_blocking_issues)
        json.dumps(audit.to_dict())

    def test_edge_table_reports_conservative_residuals(self) -> None:
        audit = audit_pdf_bytes(_make_table_pdf(residual=True))

        self.assertEqual(audit.tables_without_th, 0)
        self.assertTrue(audit.tables_th_invalid_scope)
        self.assertTrue(audit.tables_th_duplicate_id)
        self.assertTrue(audit.data_cells_missing_headers)
        self.assertTrue(audit.data_cells_malformed_headers)
        self.assertTrue(audit.data_cells_unresolved_headers)
        self.assertTrue(audit.invalid_row_child_roles)
        self.assertTrue(audit.tables_with_inconsistent_row_widths)
        json.dumps(audit.to_dict())

    def test_row_groups_are_valid_table_children(self) -> None:
        original = _make_table_pdf(residual=False)
        with pikepdf.open(io.BytesIO(original)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            rows = list(table["/K"])
            head = pikepdf.Dictionary(
                {"/S": pikepdf.Name("/THead"), "/K": pikepdf.Array(rows[:1])}
            )
            body = pikepdf.Dictionary(
                {"/S": pikepdf.Name("/TBody"), "/K": pikepdf.Array(rows[1:])}
            )
            caption = pikepdf.Dictionary({"/S": pikepdf.Name("/Caption")})
            table["/K"] = pikepdf.Array([caption, head, body])
            output = io.BytesIO()
            pdf.save(output)

        audit = audit_pdf_bytes(output.getvalue())

        self.assertEqual(audit.table_count, 1)
        self.assertEqual(audit.invalid_row_child_roles, [])
        self.assertEqual(audit.tables_with_inconsistent_row_widths, [])

    def test_non_row_inside_row_group_is_flagged(self) -> None:
        original = _make_table_pdf(residual=False)
        with pikepdf.open(io.BytesIO(original)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            rows = list(table["/K"])
            stray = pikepdf.Dictionary({"/S": pikepdf.Name("/P")})
            body = pikepdf.Dictionary(
                {"/S": pikepdf.Name("/TBody"), "/K": pikepdf.Array(rows + [stray])}
            )
            table["/K"] = pikepdf.Array([body])
            output = io.BytesIO()
            pdf.save(output)

        audit = audit_pdf_bytes(output.getvalue())

        self.assertEqual(len(audit.invalid_row_child_roles), 1)
        self.assertIn("TBody child 3: /P (expected /TR)", audit.invalid_row_child_roles[0])

    def test_table_residuals_are_nonblocking(self) -> None:
        original = _make_table_pdf(residual=False)
        with pikepdf.open(io.BytesIO(original)) as pdf:
            table = pdf.Root["/StructTreeRoot"]["/K"][0]
            table["/K"][0]["/K"][0]["/S"] = pikepdf.Name("/TD")
            output = io.BytesIO()
            pdf.save(output)
            original = output.getvalue()

        audit = audit_pdf_bytes(original)
        self.assertEqual(audit.tables_without_th, 1)
        self.assertFalse(audit.has_blocking_issues)


if __name__ == "__main__":
    unittest.main()
