from __future__ import annotations

import logging
import subprocess
from datetime import date, timedelta

import boto3

from app.core.config import Settings

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 15
_DUMP_SUFFIX = ".dump"


def _s3_key(prefix: str, dump_date: date) -> str:
    return f"{prefix}pov-{dump_date.isoformat()}{_DUMP_SUFFIX}"


def _parse_date_from_key(key: str) -> date | None:
    try:
        name = key.rsplit("/", 1)[-1]
        date_str = name.removeprefix("pov-").removesuffix(_DUMP_SUFFIX)
        return date.fromisoformat(date_str)
    except (ValueError, AttributeError):
        return None


def upload_etl_dump(settings: Settings, dump_date: date | None = None) -> dict:
    """Run pg_dump, stream output directly to S3, then prune dumps older than 15 days."""
    if not settings.s3_dump_bucket:
        logger.info("S3_DUMP_BUCKET not configured — skipping ETL dump upload")
        return {"skipped": True, "reason": "S3_DUMP_BUCKET not set"}

    if dump_date is None:
        dump_date = date.today()

    s3 = boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
    )

    key = _s3_key(settings.s3_dump_prefix, dump_date)

    proc = subprocess.Popen(
        ["pg_dump", "--format=custom", f"--dbname={settings.database_url}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        s3.upload_fileobj(proc.stdout, settings.s3_dump_bucket, key)
    finally:
        proc.stdout.close()
        stderr_out = proc.stderr.read().decode(errors="replace")
        proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed (exit {proc.returncode}): {stderr_out.strip()}")

    logger.info("ETL dump uploaded to s3://%s/%s", settings.s3_dump_bucket, key)

    deleted = _delete_old_dumps(s3, settings.s3_dump_bucket, settings.s3_dump_prefix, dump_date)

    return {
        "s3_key": f"s3://{settings.s3_dump_bucket}/{key}",
        "deleted_old_dumps": deleted,
    }


def _delete_old_dumps(s3, bucket: str, prefix: str, reference_date: date) -> list[str]:
    cutoff = reference_date - timedelta(days=_RETENTION_DAYS)
    paginator = s3.get_paginator("list_objects_v2")
    to_delete = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            dump_date = _parse_date_from_key(obj["Key"])
            if dump_date is not None and dump_date < cutoff:
                to_delete.append(obj["Key"])

    if to_delete:
        s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in to_delete]},
        )
        logger.info("Deleted %d old ETL dump(s): %s", len(to_delete), to_delete)

    return to_delete
