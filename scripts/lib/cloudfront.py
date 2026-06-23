import time

import boto3


def invalidate_paths(distribution_id: str, paths: list[str]) -> str | None:
    if not distribution_id or not paths:
        return None
    client = boto3.client("cloudfront")
    response = client.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": len(paths), "Items": paths},
            "CallerReference": str(int(time.time() * 1000)),
        },
    )
    return response["Invalidation"]["Id"]
