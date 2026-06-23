import os

ENV_BUCKETS = {
    "dev": "channels-data-dev",
    "prod": "channels-data-prod",
}

ENV_CLOUDFRONT = {
    "dev": "E27O7BO97BHXFO",
    "prod": os.environ.get("CHANNELS_CLOUDFRONT_DISTRIBUTION_ID_PROD", ""),
}


def channels_bucket(env: str) -> str:
    return os.environ.get("CHANNELS_DATA_BUCKET") or ENV_BUCKETS[env]


def cloudfront_distribution_id(env: str) -> str:
    return os.environ.get("CHANNELS_CLOUDFRONT_DISTRIBUTION_ID") or ENV_CLOUDFRONT.get(env, "")


def a11y_bucket() -> str:
    bucket = os.environ.get("A11Y_BUCKET")
    if bucket:
        return bucket
    raise RuntimeError(
        "Set A11Y_BUCKET to the pdfaccessibility CDK bucket name (e.g. pdfaccessibilitybucket1-...)"
    )


def state_machine_arn() -> str:
    arn = os.environ.get("STATE_MACHINE_ARN")
    if arn:
        return arn
    raise RuntimeError(
        "Set STATE_MACHINE_ARN to the PdfAccessibilityRemediationWorkflow ARN"
    )
