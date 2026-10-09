"""Document object storage: one interface, local and S3-compatible implementations."""

from __future__ import annotations

from typing import assert_never

from docintel.core.config import Settings, StorageBackendName
from docintel.storage.base import (
    DocumentStorage,
    ObjectNotFoundError,
    StorageError,
    StorageKeyError,
    StorageUnavailableError,
    StoredObject,
    document_object_key,
    page_preview_key,
)
from docintel.storage.local import LocalStorage
from docintel.storage.s3 import S3Storage

__all__ = [
    "DocumentStorage",
    "LocalStorage",
    "ObjectNotFoundError",
    "S3Storage",
    "StorageError",
    "StorageKeyError",
    "StorageUnavailableError",
    "StoredObject",
    "build_storage",
    "document_object_key",
    "page_preview_key",
]


def build_storage(settings: Settings) -> DocumentStorage:
    match settings.storage_backend:
        case StorageBackendName.LOCAL:
            return LocalStorage(settings.storage_local_root)
        case StorageBackendName.S3:
            if settings.s3_bucket is None:  # guarded by Settings validation as well
                msg = "S3_BUCKET is required when STORAGE_BACKEND=s3"
                raise ValueError(msg)
            secret = settings.s3_secret_access_key
            return S3Storage(
                bucket=settings.s3_bucket,
                key_prefix=settings.s3_key_prefix,
                endpoint_url=settings.s3_endpoint_url,
                region=settings.s3_region,
                access_key_id=settings.s3_access_key_id,
                secret_access_key=secret.get_secret_value() if secret else None,
            )
        case _:
            assert_never(settings.storage_backend)
