import json
import time
from typing import Any

import pymupdf
from botocore.exceptions import ClientError


def split_pdf_into_chunks(
    pdf_bytes: bytes,
    source_key: str,
    s3_client,
    bucket_name: str,
    pages_per_chunk: int = 200,
) -> list[dict[str, str]]:
    file_basename = source_key.split("/")[-1].rsplit(".", 1)[0]
    chunks: list[dict[str, str]] = []
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        num_pages = doc.page_count
        for start in range(0, num_pages, pages_per_chunk):
            end = min(start + pages_per_chunk, num_pages)
            chunk_index = start // pages_per_chunk + 1
            page_filename = f"{file_basename}_chunk_{chunk_index}.pdf"
            s3_key = f"temp/{file_basename}/{page_filename}"

            if start == 0 and end == num_pages:
                # Avoid re-serializing small PDFs; pypdf can fail on malformed numbers.
                chunk_bytes = pdf_bytes
            else:
                chunk_doc = pymupdf.open()
                try:
                    chunk_doc.insert_pdf(doc, from_page=start, to_page=end - 1)
                    chunk_bytes = chunk_doc.tobytes()
                finally:
                    chunk_doc.close()

            s3_client.put_object(
                Bucket=bucket_name,
                Key=s3_key,
                Body=chunk_bytes,
                ContentType="application/pdf",
            )
            chunks.append(
                {
                    "s3_bucket": bucket_name,
                    "s3_key": s3_key,
                    "chunk_key": s3_key,
                }
            )
    finally:
        doc.close()

    return chunks


def wait_for_execution(stepfunctions_client, execution_arn: str, timeout_seconds: int = 7200) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = stepfunctions_client.describe_execution(executionArn=execution_arn)
        status = response["status"]
        if status == "SUCCEEDED":
            return status
        if status in ("FAILED", "TIMED_OUT", "ABORTED"):
            raise RuntimeError(
                f"Step Function {execution_arn} ended with status {status}: "
                f"{response.get('cause', '')} {response.get('error', '')}"
            )
        time.sleep(15)
    raise TimeoutError(f"Step Function did not finish within {timeout_seconds}s: {execution_arn}")


def wait_for_result_object(
    s3_client,
    bucket: str,
    result_key: str,
    timeout_seconds: int = 7200,
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            s3_client.head_object(Bucket=bucket, Key=result_key)
            return
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code in ("404", "NoSuchKey", "NotFound"):
                time.sleep(15)
                continue
            raise
    raise TimeoutError(f"Timed out waiting for s3://{bucket}/{result_key}")


def remediate_preview_pdf(
    s3_client,
    stepfunctions_client,
    a11y_bucket: str,
    state_machine_arn: str,
    pdf_bytes: bytes,
    course_id: str,
    topic_id: str,
    topic_title: str,
) -> bytes:
    # Use migrate/ prefix (not pdf/) so the bucket's pdf/ S3 trigger does not start a
    # duplicate Step Function execution without channelsJob.
    source_key = f"migrate/{course_id}/{topic_id}.pdf"
    channels_sidecar_key = f"migrate/{course_id}/{topic_id}.channels.json"
    channels_job = {
        "skipTitleLlm": True,
        "topicTitle": topic_title,
    }
    s3_client.put_object(
        Bucket=a11y_bucket,
        Key=source_key,
        Body=pdf_bytes,
        ContentType="application/pdf",
    )
    s3_client.put_object(
        Bucket=a11y_bucket,
        Key=channels_sidecar_key,
        Body=json.dumps(channels_job).encode("utf-8"),
        ContentType="application/json",
    )

    chunks = split_pdf_into_chunks(pdf_bytes, source_key, s3_client, a11y_bucket)
    topic_folder = source_key.split("/")[-1].rsplit(".", 1)[0]
    s3_client.put_object(
        Bucket=a11y_bucket,
        Key=f"temp/{topic_folder}/{topic_folder}.channels.json",
        Body=json.dumps(channels_job).encode("utf-8"),
        ContentType="application/json",
    )
    execution_input: dict[str, Any] = {
        "chunks": chunks,
        "s3_bucket": a11y_bucket,
        "source_pdf_key": source_key,
        "channelsJob": channels_job,
    }
    execution = stepfunctions_client.start_execution(
        stateMachineArn=state_machine_arn,
        input=json.dumps(execution_input),
    )
    wait_for_execution(stepfunctions_client, execution["executionArn"])

    result_key = f"result/COMPLIANT_{topic_id}.pdf"
    wait_for_result_object(s3_client, a11y_bucket, result_key)

    response = s3_client.get_object(Bucket=a11y_bucket, Key=result_key)
    return response["Body"].read()
