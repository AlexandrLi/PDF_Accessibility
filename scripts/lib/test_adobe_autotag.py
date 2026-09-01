"""Tests for Adobe auto-tag credential handling."""

from __future__ import annotations

import io
import json
import unittest

import pikepdf

from lib.accessibility_course_workflow import render_hashes, validate_pdf
from lib.adobe_autotag import load_adobe_credentials, normalize_pdf_for_autotag


class FakeSecretsClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.secret_id: str | None = None

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
        self.secret_id = SecretId
        return {"SecretString": json.dumps(self.payload)}


class AdobeAutotagTests(unittest.TestCase):
    def test_normalization_preserves_pages_and_rendering(self) -> None:
        pdf = pikepdf.new()
        pdf.add_blank_page(page_size=(200, 300))
        source = io.BytesIO()
        pdf.save(source)
        original = source.getvalue()

        normalized = normalize_pdf_for_autotag(original)

        self.assertEqual(
            validate_pdf(normalized)["pages"],
            validate_pdf(original)["pages"],
        )
        self.assertEqual(render_hashes(normalized), render_hashes(original))

    def test_loads_existing_pdf_services_credentials(self) -> None:
        client = FakeSecretsClient(
            {
                "client_credentials": {
                    "PDF_SERVICES_CLIENT_ID": "client",
                    "PDF_SERVICES_CLIENT_SECRET": "secret",
                }
            }
        )

        credentials = load_adobe_credentials(client)

        self.assertEqual(credentials, ("client", "secret"))
        self.assertEqual(client.secret_id, "/myapp/client_credentials")

    def test_rejects_missing_pdf_services_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "credentials are missing"):
            load_adobe_credentials(FakeSecretsClient({}))


if __name__ == "__main__":
    unittest.main()
