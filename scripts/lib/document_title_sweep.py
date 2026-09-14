"""Repair the Acrobat "Title" failure by naming the document in its metadata."""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass

import pikepdf

from lib.adobe_autotag import apply_document_title


@dataclass
class DocumentTitleRepairResult:
    title_before: str
    title_after: str
    display_doc_title_before: bool
    actions: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _title_surfaces(pdf_bytes: bytes) -> tuple[str, bool, str | None]:
    """The document's title, whether it is displayed, and its XMP title.

    The XMP title is None when the document carries no XMP stream at all,
    which needs no repair of its own.
    """
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        info = pdf.trailer.get("/Info")
        title = "" if info is None else str(info.get("/Title") or "").strip()
        preferences = pdf.Root.get("/ViewerPreferences")
        displayed = (
            isinstance(preferences, pikepdf.Dictionary)
            and preferences.get("/DisplayDocTitle") is True
        )
        if pdf.Root.get("/Metadata") is None:
            return title, displayed, None
        with pdf.open_metadata(
            update_docinfo=False,
            set_pikepdf_as_editor=False,
        ) as metadata:
            return title, displayed, str(metadata.get("dc:title") or "").strip()


def repair_document_title(
    pdf_bytes: bytes,
    *,
    title: str | None = None,
) -> tuple[bytes, DocumentTitleRepairResult]:
    """Give the document a title and show it in the window title bar.

    Acrobat fails "Title" unless the document has a non-empty title *and*
    ``/ViewerPreferences /DisplayDocTitle`` is true.  Only metadata is
    touched, so the rendered pages stay byte-identical.

    `title` is the authoritative name from the course TOC, used only when the
    document names itself nothing; an author's own title is kept.  Without a
    supplied title there is nothing to write, and a document already titled
    and displaying it is left alone, which keeps a second pass a no-op.
    """
    wanted = (title or "").strip()
    before, displayed, xmp_before = _title_surfaces(pdf_bytes)
    unchanged = DocumentTitleRepairResult(
        title_before=before,
        title_after=before,
        display_doc_title_before=displayed,
        actions=[],
    )

    if not wanted:
        unchanged.actions = ["no title supplied"]
        return pdf_bytes, unchanged
    if before and displayed and xmp_before != "":
        return pdf_bytes, unchanged

    actions: list[str] = []
    if not before:
        actions.append(f"set /Title to {wanted!r}")
    if not displayed:
        actions.append("set /DisplayDocTitle to true")
    if xmp_before == "":
        actions.append("set XMP dc:title")

    # An author's own title outranks the TOC name; the empty ones get the TOC
    # name.  Both are written to all three surfaces Acrobat reads.
    resolved = before or wanted
    return apply_document_title(pdf_bytes, resolved), DocumentTitleRepairResult(
        title_before=before,
        title_after=resolved,
        display_doc_title_before=displayed,
        actions=actions,
    )
