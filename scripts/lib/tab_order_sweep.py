"""Repair Acrobat tab-order failures by setting every page to /S order."""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass

import pikepdf


@dataclass
class TabOrderRepairResult:
    pages_found: int
    pages_updated: int
    actions: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def repair_tab_order(pdf_bytes: bytes) -> tuple[bytes, TabOrderRepairResult]:
    """Set every page's tab order to the PDF structure order.

    Only the page dictionary's ``/Tabs`` entry is changed.  Pages that already
    use ``/S`` are left untouched so a second pass is a no-op.
    """
    actions: list[str] = []
    pages_updated = 0

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        pages_found = len(pdf.pages)
        for page_number, page in enumerate(pdf.pages, start=1):
            if page.get("/Tabs") == pikepdf.Name("/S"):
                continue
            page["/Tabs"] = pikepdf.Name("/S")
            pages_updated += 1
            actions.append(f"page {page_number}: set /Tabs to /S")

        if pages_updated == 0:
            return pdf_bytes, TabOrderRepairResult(
                pages_found=pages_found,
                pages_updated=0,
                actions=[],
            )

        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue(), TabOrderRepairResult(
            pages_found=pages_found,
            pages_updated=pages_updated,
            actions=actions,
        )
