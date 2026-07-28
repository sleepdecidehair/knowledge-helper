"""RainS3 / S3 兼容对象存储，存放 knowledge/ 原始文件。

未配置 S3 时回退到本地文件系统。data/ 索引和元数据始终保留在本地。
"""

import io
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class S3Storage:
    """最小化 S3 封装：上传、下载、删除、列举。"""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
    ):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self._client = None
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
            )
        return self._client

    def enabled(self) -> bool:
        return bool(self.endpoint and self._access_key and self._secret_key and self.bucket)

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            self.client.create_bucket(Bucket=self.bucket)

    def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self.ensure_bucket()
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.info("S3 upload: %s (%d bytes)", key, len(data))
        return key

    def download(self, key: str) -> Optional[bytes]:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
            return obj["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            logger.warning("S3 download failed: %s — %s", key, exc)
            return None

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            logger.info("S3 delete: %s", key)
        except Exception as exc:
            logger.warning("S3 delete failed: %s — %s", key, exc)

    def list_keys(self, prefix: str = "") -> list[str]:
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            keys: list[str] = []
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
            return keys
        except Exception as exc:
            logger.warning("S3 list failed: %s", exc)
            return []


def create_s3_storage() -> Optional[S3Storage]:
    """从环境变量构造 S3 客户端；配置不完整时返回 None。"""
    import os

    endpoint = os.getenv("S3_ENDPOINT", "").strip()
    access_key = os.getenv("S3_ACCESS_KEY", "").strip()
    secret_key = os.getenv("S3_SECRET_KEY", "").strip()
    bucket = os.getenv("S3_BUCKET", "knowledge").strip()

    if not endpoint or not access_key or not secret_key:
        return None

    s3 = S3Storage(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
    )
    if not s3.enabled():
        return None
    return s3
