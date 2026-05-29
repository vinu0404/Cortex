import io
import logging

import boto3
from botocore.config import Config

from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

_PART_SIZE = 5 * 1024 * 1024  # 5 MB


def _get_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.B2_ENDPOINT,
        region_name=settings.B2_REGION,
        aws_access_key_id=settings.B2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.B2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
    )


def build_kb_storage_key(kb_id: str, doc_id: str, filename: str) -> str:
    return f"kb/{kb_id}/{doc_id}/{filename}"


def multipart_upload_file(file_path: str, storage_key: str, content_type: str) -> None:
    client = _get_client()
    mpu = client.create_multipart_upload(Bucket=settings.B2_BUCKET, Key=storage_key, ContentType=content_type)
    upload_id = mpu["UploadId"]
    parts = []
    part_num = 1
    try:
        with open(file_path, "rb") as f:
            while True:
                data = f.read(_PART_SIZE)
                if not data:
                    break
                resp = client.upload_part(
                    Bucket=settings.B2_BUCKET, Key=storage_key,
                    UploadId=upload_id, PartNumber=part_num, Body=data,
                )
                parts.append({"ETag": resp["ETag"], "PartNumber": part_num})
                part_num += 1
        client.complete_multipart_upload(
            Bucket=settings.B2_BUCKET, Key=storage_key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        client.abort_multipart_upload(Bucket=settings.B2_BUCKET, Key=storage_key, UploadId=upload_id)
        raise


def multipart_upload_bytes(data: bytes, storage_key: str, content_type: str) -> None:
    client = _get_client()
    mpu = client.create_multipart_upload(Bucket=settings.B2_BUCKET, Key=storage_key, ContentType=content_type)
    upload_id = mpu["UploadId"]
    parts = []
    part_num = 1
    try:
        stream = io.BytesIO(data)
        while True:
            chunk = stream.read(_PART_SIZE)
            if not chunk:
                break
            resp = client.upload_part(
                Bucket=settings.B2_BUCKET, Key=storage_key,
                UploadId=upload_id, PartNumber=part_num, Body=chunk,
            )
            parts.append({"ETag": resp["ETag"], "PartNumber": part_num})
            part_num += 1
        client.complete_multipart_upload(
            Bucket=settings.B2_BUCKET, Key=storage_key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        client.abort_multipart_upload(Bucket=settings.B2_BUCKET, Key=storage_key, UploadId=upload_id)
        raise


def download_file(storage_key: str) -> bytes:
    client = _get_client()
    resp = client.get_object(Bucket=settings.B2_BUCKET, Key=storage_key)
    return resp["Body"].read()


def generate_presigned_url(storage_key: str) -> str:
    client = _get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.B2_BUCKET, "Key": storage_key},
        ExpiresIn=settings.B2_PRESIGN_EXPIRY,
    )


def delete_file(storage_key: str) -> None:
    try:
        client = _get_client()
        client.delete_object(Bucket=settings.B2_BUCKET, Key=storage_key)
    except Exception:
        logger.warning("Failed to delete B2 file: %s", storage_key, exc_info=True)
