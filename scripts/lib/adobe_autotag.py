"""Adobe PDF Services auto-tagging for structurally untagged PDFs."""

from __future__ import annotations

import io
import json
from typing import Any

import boto3
import pymupdf
from adobe.pdfservices.operation.auth.service_principal_credentials import (
    ServicePrincipalCredentials,
)
from adobe.pdfservices.operation.io.cloud_asset import CloudAsset
from adobe.pdfservices.operation.io.stream_asset import StreamAsset
from adobe.pdfservices.operation.pdf_services import ClientConfig, PDFServices
from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
from adobe.pdfservices.operation.pdfjobs.jobs.autotag_pdf_job import AutotagPDFJob
from adobe.pdfservices.operation.pdfjobs.jobs.pdf_accessibility_checker_job import (
    PDFAccessibilityCheckerJob,
)
from adobe.pdfservices.operation.pdfjobs.params.autotag_pdf.autotag_pdf_params import (
    AutotagPDFParams,
)
from adobe.pdfservices.operation.pdfjobs.result.autotag_pdf_result import (
    AutotagPDFResult,
)
from adobe.pdfservices.operation.pdfjobs.result.pdf_accessibility_checker_result import (
    PDFAccessibilityCheckerResult,
)
from pypdf import PdfReader, PdfWriter


DEFAULT_SECRET_ID = "/myapp/client_credentials"


def normalize_pdf_for_autotag(pdf_bytes: bytes) -> bytes:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    if reader.metadata:
        writer.add_metadata(
            {
                str(key): str(value)
                for key, value in reader.metadata.items()
                if value is not None
            }
        )
    writer.create_viewer_preferences()
    writer.viewer_preferences.display_doctitle = True
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def ocr_pdf_for_autotag(pdf_bytes: bytes, *, dpi: int = 300) -> bytes:
    source = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    output = pymupdf.open()
    for page in source:
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        ocr_page = pymupdf.open(
            stream=pixmap.pdfocr_tobytes(language="eng", compress=True),
            filetype="pdf",
        )
        output.insert_pdf(ocr_page)
        ocr_page.close()
    metadata = {
        key: value
        for key, value in source.metadata.items()
        if value is not None
    }
    if metadata:
        output.set_metadata(metadata)
    result = output.tobytes(garbage=4, deflate=True)
    output.close()
    source.close()
    return result


def load_adobe_credentials(
    secrets_client: Any | None = None,
    *,
    secret_id: str = DEFAULT_SECRET_ID,
) -> tuple[str, str]:
    client = secrets_client or boto3.client("secretsmanager", region_name="us-east-1")
    response = client.get_secret_value(SecretId=secret_id)
    secret = json.loads(response["SecretString"])
    credentials = secret.get("client_credentials") or {}
    client_id = credentials.get("PDF_SERVICES_CLIENT_ID")
    client_secret = credentials.get("PDF_SERVICES_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("Adobe PDF Services credentials are missing")
    return str(client_id), str(client_secret)


def autotag_pdf(
    pdf_bytes: bytes,
    *,
    client_id: str,
    client_secret: str,
) -> tuple[bytes, bytes]:
    credentials = ServicePrincipalCredentials(
        client_id=client_id,
        client_secret=client_secret,
    )
    pdf_services = PDFServices(
        credentials=credentials,
        client_config=ClientConfig(connect_timeout=8000, read_timeout=40000),
    )
    input_asset = pdf_services.upload(
        input_stream=pdf_bytes,
        mime_type=PDFServicesMediaType.PDF,
    )
    job = AutotagPDFJob(
        input_asset=input_asset,
        autotag_pdf_params=AutotagPDFParams(
            generate_report=True,
            shift_headings=True,
        ),
    )
    location = pdf_services.submit(job)
    response = pdf_services.get_job_result(location, AutotagPDFResult)
    result = response.get_result()
    tagged_asset: CloudAsset = result.get_tagged_pdf()
    report_asset: CloudAsset = result.get_report()
    tagged_stream: StreamAsset = pdf_services.get_content(tagged_asset)
    report_stream: StreamAsset = pdf_services.get_content(report_asset)
    return (
        tagged_stream.get_input_stream(),
        report_stream.get_input_stream(),
    )


def autotag_pdf_from_secret(
    pdf_bytes: bytes,
    secrets_client: Any | None = None,
) -> tuple[bytes, bytes]:
    client_id, client_secret = load_adobe_credentials(secrets_client)
    return autotag_pdf(
        pdf_bytes,
        client_id=client_id,
        client_secret=client_secret,
    )


def check_pdf_accessibility(
    pdf_bytes: bytes,
    *,
    client_id: str,
    client_secret: str,
) -> bytes:
    credentials = ServicePrincipalCredentials(
        client_id=client_id,
        client_secret=client_secret,
    )
    pdf_services = PDFServices(
        credentials=credentials,
        client_config=ClientConfig(connect_timeout=8000, read_timeout=40000),
    )
    input_asset = pdf_services.upload(
        input_stream=pdf_bytes,
        mime_type=PDFServicesMediaType.PDF,
    )
    location = pdf_services.submit(
        PDFAccessibilityCheckerJob(input_asset=input_asset)
    )
    response = pdf_services.get_job_result(
        location,
        PDFAccessibilityCheckerResult,
    )
    report_asset: CloudAsset = response.get_result().get_report()
    report_stream: StreamAsset = pdf_services.get_content(report_asset)
    return report_stream.get_input_stream()


def check_pdf_accessibility_from_secret(
    pdf_bytes: bytes,
    secrets_client: Any | None = None,
) -> bytes:
    client_id, client_secret = load_adobe_credentials(secrets_client)
    return check_pdf_accessibility(
        pdf_bytes,
        client_id=client_id,
        client_secret=client_secret,
    )
