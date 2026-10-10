"""Filesystem storage for local development and single-host deployments."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import anyio

from docintel.core.config import StorageBackendName
from docintel.storage.base import (
    DEFAULT_CHUNK_SIZE,
    ListedObject,
    ObjectNotFoundError,
    StorageKeyError,
    StoredObject,
    validate_key,
)

_FILE_MODE = 0o640


class LocalStorage:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._root = root.resolve()

    @property
    def backend(self) -> StorageBackendName:
        return StorageBackendName.LOCAL

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = (self._root / key).resolve()
        # Resolving follows symlinks, so a planted link cannot point outside the root either.
        if not path.is_relative_to(self._root):
            msg = f"storage key escapes the storage root: {key!r}"
            raise StorageKeyError(msg)
        return path

    def _put_sync(self, key: str, source: Path) -> StoredObject:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as reader, temporary.open("wb") as writer:
                shutil.copyfileobj(reader, writer, DEFAULT_CHUNK_SIZE)
                writer.flush()
                os.fsync(writer.fileno())
            temporary.chmod(_FILE_MODE)
            # Atomic on POSIX: readers see either the old object or the complete new one.
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return StoredObject(key=key, size_bytes=destination.stat().st_size)

    async def put_file(self, key: str, source: Path, *, content_type: str) -> StoredObject:
        return await asyncio.to_thread(self._put_sync, key, source)

    async def open_stream(
        self, key: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> AsyncIterator[bytes]:
        path = self._path(key)
        try:
            handle = await anyio.open_file(path, "rb")
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc
        async with handle:
            while chunk := await handle.read(chunk_size):
                yield chunk

    def _download_sync(self, key: str, destination: Path) -> None:
        source = self._path(key)
        try:
            shutil.copyfile(source, destination)
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc

    async def download_to(self, key: str, destination: Path) -> None:
        await asyncio.to_thread(self._download_sync, key, destination)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(path.unlink, missing_ok=True)

    async def exists(self, key: str) -> bool:
        path = self._path(key)
        return await asyncio.to_thread(path.is_file)

    def _list_sync(self, prefix: str) -> list[ListedObject]:
        listed = []
        for directory, _, files in os.walk(self._root):
            for name in files:
                if name.startswith("."):  # a write in progress (see _put_sync)
                    continue
                path = Path(directory) / name
                key = path.relative_to(self._root).as_posix()
                if not key.startswith(prefix):
                    continue
                stat = path.stat()
                listed.append(
                    ListedObject(
                        key=key,
                        size_bytes=stat.st_size,
                        modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                    )
                )
        return listed

    async def list_objects(self, prefix: str) -> AsyncIterator[ListedObject]:
        for listed in await asyncio.to_thread(self._list_sync, prefix):
            yield listed
