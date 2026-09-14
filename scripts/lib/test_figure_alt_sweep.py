"""Tests for the fallback figure alt repair."""

from __future__ import annotations

import io
import unittest

import pikepdf

from lib.figure_alt_sweep import repair_missing_figure_alt


def _elem(name: str, **extra: object) -> pikepdf.Dictionary:
    data = {"/Type": pikepdf.Name("/StructElem"), "/S": pikepdf.Name(f"/{name}")}
    for key, value in extra.items():
        data[f"/{key}"] = pikepdf.String(value) if isinstance(value, str) else value
    return pikepdf.Dictionary(data)


def _pdf_with_figure(*children: pikepdf.Dictionary, alt: str | None = None) -> bytes:
    pdf = pikepdf.new()
    figure = _elem("Figure")
    if alt is not None:
        figure["/Alt"] = pikepdf.String(alt)
    textbox = _elem("Textbox")
    textbox["/K"] = pikepdf.Array(list(children[1:]))
    for mcid, child in enumerate(children):
        child["/K"] = pikepdf.Array([mcid])
    figure["/K"] = pikepdf.Array([children[0], textbox] if children else [])
    document = _elem("Document")
    document["/K"] = pikepdf.Array([figure])
    pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary(
        {"/Type": pikepdf.Name("/StructTreeRoot"), "/K": pikepdf.Array([document])}
    )
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


def _figure_and_descendants(pdf_bytes: bytes) -> tuple[pikepdf.Dictionary, list[pikepdf.Dictionary]]:
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    figure = pdf.Root.StructTreeRoot.K[0].K[0]
    descendants: list[pikepdf.Dictionary] = []

    def walk(obj: pikepdf.Dictionary) -> None:
        for kid in obj.get("/K", []):
            if isinstance(kid, pikepdf.Dictionary) and "/S" in kid:
                descendants.append(kid)
                walk(kid)

    walk(figure)
    return figure, descendants


class RepairMissingFigureAltTests(unittest.TestCase):
    def test_clears_descendant_actualtext_before_stamping_alt(self) -> None:
        source = _pdf_with_figure(
            _elem("Span", ActualText=" "),
            _elem("Span", ActualText="ሺ"),
            _elem("Span", Alt="ሻ"),
        )

        repaired_bytes, repaired = repair_missing_figure_alt(source)

        figure, descendants = _figure_and_descendants(repaired_bytes)
        self.assertEqual(repaired, [1])
        self.assertEqual(str(figure.Alt), "Figure 1")
        for element in descendants:
            self.assertNotIn("/Alt", element)
            self.assertNotIn("/ActualText", element)

    def test_folds_readable_descendant_text_into_the_fallback(self) -> None:
        source = _pdf_with_figure(
            _elem("Span", ActualText="Intercept "),
            _elem("Span", ActualText=" "),
            _elem("Span", ActualText="Variable"),
        )

        repaired_bytes, _ = repair_missing_figure_alt(source)

        figure, _ = _figure_and_descendants(repaired_bytes)
        self.assertEqual(str(figure.Alt), "Figure 1: Intercept Variable")
        self.assertEqual(str(figure.Contents), "Figure 1: Intercept Variable")

    def test_leaves_figures_with_alt_and_their_descendants_alone(self) -> None:
        source = _pdf_with_figure(_elem("Span", ActualText="x"), alt="A graph")

        repaired_bytes, repaired = repair_missing_figure_alt(source)

        self.assertEqual(repaired, [])
        self.assertEqual(repaired_bytes, source)

    def test_second_pass_returns_input_unchanged(self) -> None:
        source = _pdf_with_figure(_elem("Span", ActualText=" "))

        first, _ = repair_missing_figure_alt(source)
        second, repaired = repair_missing_figure_alt(first)

        self.assertEqual(repaired, [])
        self.assertEqual(second, first)

    def test_drops_a_figure_with_no_page_content_instead_of_stamping_alt(self) -> None:
        source = _pdf_with_figure()

        repaired_bytes, repaired = repair_missing_figure_alt(source)

        pdf = pikepdf.open(io.BytesIO(repaired_bytes))
        document = pdf.Root.StructTreeRoot.K[0]
        self.assertEqual(repaired, [])
        self.assertEqual(len(document.K), 0)
        self.assertNotEqual(repaired_bytes, source)


if __name__ == "__main__":
    unittest.main()
