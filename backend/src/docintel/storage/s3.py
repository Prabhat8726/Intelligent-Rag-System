"""S3-compatible object storage (AWS S3, Cloudflare R2, MinIO, Ceph, ...).

boto3 is synchronous; calls run in worker threads (boto3 clients are thread-safe).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from docintel.core.config import StorageBackendName
from docintel.storage.base import (
    DEFAULT_CHUNK_SIZE,
    ObjectNotFoundError,
    StorageError,
    StoredObject,
    StorageUnavailableError,
    validate_key,
)

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def _translate(exc: Exception, key: str) -> StorageError:
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in _NOT_FOUND_CODES or status == 404:
            return ObjectNotFoundError(key)
        if status >= 500 or code in {"SlowDown", "RequestTimeout", "Throttling"}:
            return StorageUnavailableError(f"S3 unavailable ({code or status})")
        return StorageError(f"S3 request failed ({code or status})")
    return StorageUnavailableError(f"S3 unreachable: {type(exc).__name__}")


class S3Storage:
    def __init__(
        self,
        *,
        bucket: str,
        key_prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        client: S3Client | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = key_prefix.strip("/")
        self._client: S3Client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
        )

    @property
    def backend(self) -> StorageBackendName:
        return StorageBackendName.S3

    def _object_key(self, key: str) -> str:
        validate_key(key)
        return f"{self._prefix}/{key}" if self._prefix else key

    async def _call[R](self, key: str, operation: Callable[[], R]) -> R:
        try:
            return await asyncio.to_thread(operation)
        except (ClientError, BotoCoreError) as exc:
            raise _translate(exc, key) from exc

    async def put_file(self, key: str, source: Path, *, content_type: str) -> StoredObject:
        object_key = self._object_key(key)
        await self._call(
            key,
            lambda: self._client.upload_file(
                str(source), self._bucket, object_key, ExtraArgs={"ContentType": content_type}
            ),
        )
        return StoredObject(key=key, size_bytes=source.stat().st_size)

    async def open_stream(
        self, key: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> AsyncIterator[bytes]:
        object_key = self._object_key(key)
        response: dict[str, Any] = await self._call(
            key, lambda: dict(self._client.get_object(Bucket=self._bucket, Key=object_key))
        )
        body = response["Body"]
        try:
            while chunk := await self._call(key, lambda: body.read(chunk_size)):
                yield chunk
        finally:
            body.close()

    async def download_to(self, key: str, destination: Path) -> None:
        object_key = self._object_key(key)
        await self._call(
            key, lambda: self._client.download_file(self._bucket, object_key, str(destination))
        )

    async def delete(self, key: str) -> None:
        object_key = self._object_key(key)
        await self._call(key, lambda: self._client.delete_object(Bucket=self._bucket, Key=object_key))

    async def exists(self, key: str) -> bool:
        object_key = self._object_key(key)
        try:
            await self._call(
                key, lambda: self._client.head_object(Bucket=self._bucket, Key=object_key)
            )
        except ObjectNotFoundError:
            return False
        return True
