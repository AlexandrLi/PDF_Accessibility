"""Tests for PDF remediation helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import pymupdf

from lib.remediation import split_pdf_into_chunks


def build_pdf_bytes(page_count: int) -> bytes:
    doc = pymupdf.open()
    try:
        for _ in range(page_count):
            doc.new_page()
        return doc.tobytes()
    finally:
        doc.close()


class SplitPdfIntoChunksTests(unittest.TestCase):
    def test_single_chunk_uploads_original_bytes(self) -> None:
        pdf_bytes = build_pdf_bytes(3)
        s3_client = MagicMock()
        chunks = split_pdf_into_chunks(
            pdf_bytes,
            "migrate/psychology/topic.pdf",
            s3_client,
            "bucket",
            pages_per_chunk=200,
        )

        self.assertEqual(len(chunks), 1)
        uploaded = s3_client.put_object.call_args.kwargs
        self.assertEqual(uploaded["Body"], pdf_bytes)

    def test_multi_chunk_splits_with_pymupdf(self) -> None:
        pdf_bytes = build_pdf_bytes(5)
        s3_client = MagicMock()
        chunks = split_pdf_into_chunks(
            pdf_bytes,
            "migrate/psychology/topic.pdf",
            s3_client,
            "bucket",
            pages_per_chunk=2,
        )

        self.assertEqual(len(chunks), 3)
        bodies = [call.kwargs["Body"] for call in s3_client.put_object.call_args_list]
        page_counts: list[int] = []
        for body in bodies:
            doc = pymupdf.open(stream=body, filetype="pdf")
            try:
                page_counts.append(doc.page_count)
            finally:
                doc.close()
        self.assertEqual(page_counts, [2, 2, 1])


if __name__ == "__main__":
    unittest.main()
