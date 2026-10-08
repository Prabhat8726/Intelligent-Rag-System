"""Storage contract tests: every DocumentStorage implementation must pass the same suite.

S3 runs against moto's threaded S3 server over real HTTP (boto3 → endpoint_url), which
exercises signing, request serialization and error responses.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import boto3
import pytest
from moto.server import ThreadedMotoServer

from docintel.core.config import StorageBackendName
from docintel.storage import (
    DocumentStorage,
    LocalStorage,
    ObjectNotFoundError,
    S3Storage,
    StorageKeyError,
    build_storage,
    document_object_key,
)
from tests.conftest import make_settings

BUCKET = "docintel-test"


@pytest.fixture(scope="module")
def moto_endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    client.create_bucket(Bucket=BUCKET)
    yield endpoint
    server.stop()


@pytest.fixture(params=[StorageBackendName.LOCAL, StorageBackendName.S3])
async def storage(
    request: pytest.FixtureRequest, tmp_path: Path, moto_endpoint: str
) -> AsyncIterator[DocumentStorage]:
    if request.param == StorageBackendName.LOCAL:
        yield LocalStorage(tmp_path / "objects")
    else:
        yield S3Storage(
            bucket=BUCKET,
            key_prefix=f"run-{uuid.uuid4().hex[:6]}",
            endpoint_url=moto_endpoint,
            region="us-east-1",
            access_key_id="test",
            secret_access_key="test",
        )


def _source(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / f"src-{uuid.uuid4().hex}.bin"
    path.write_bytes(content)
    return path


async def _read_all(storage: DocumentStorage, key: str, chunk_size: int = 4) -> bytes:
    return b"".join([chunk async for chunk in storage.open_stream(key, chunk_size=chunk_size)])


async def test_put_stream_download_delete_round_trip(
    storage: DocumentStorage, tmp_path: Path
) -> None:
    key = document_object_key(uuid.uuid4(), 1, "pdf")
    content = b"%PDF-1.7 synthetic body \x00\x01\x02" * 50

    stored = await storage.put_file(key, _source(tmp_path, content), content_type="application/pdf")
    assert stored.size_bytes == len(content)
    assert await storage.exists(key)
    assert await _read_all(storage, key) == content

    destination = tmp_path / "copy.bin"
    await storage.download_to(key, destination)
    assert destination.read_bytes() == content

    await storage.delete(key)
    assert not await storage.exists(key)
    await storage.delete(key)  # idempotent


async def test_overwrite_replaces_content(storage: DocumentStorage, tmp_path: Path) -> None:
    key = document_object_key(uuid.uuid4(), 1, "png")
    await storage.put_file(key, _source(tmp_path, b"first"), content_type="image/png")
    await storage.put_file(key, _source(tmp_path, b"second version"), content_type="image/png")
    assert await _read_all(storage, key) == b"second version"


async def test_missing_object_raises_not_found(storage: DocumentStorage, tmp_path: Path) -> None:
    key = document_object_key(uuid.uuid4(), 1, "pdf")
    with pytest.raises(ObjectNotFoundError):
        await _read_all(storage, key)
    with pytest.raises(ObjectNotFoundError):
        await storage.download_to(key, tmp_path / "nothing")


@pytest.mark.parametrize(
    "key",
    [
        "../etc/passwd",
        "documents/../../secret",
        "/absolute/path",
        "a//b",
        "a/./b",
        "",
        "x" * 600,
        "documents/evil\nname",
        "documents/sp ace",
    ],
)
async def test_malicious_keys_are_rejected(
    storage: DocumentStorage, tmp_path: Path, key: str
) -> None:
    with pytest.raises(StorageKeyError):
        await storage.put_file(key, _source(tmp_path, b"x"), content_type="text/plain")
    with pytest.raises(StorageKeyError):
        await storage.exists(key)


def test_document_keys_never_contain_user_text() -> None:
    document_id = uuid.uuid4()
    assert (
        document_object_key(document_id, 3, "tiff") == f"documents/{document_id}/v3/original.tiff"
    )
    with pytest.raises(StorageKeyError):
        document_object_key(document_id, 1, "pdf/../../x")


async def test_local_storage_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"top secret")
    storage = LocalStorage(tmp_path / "root")
    (storage.root / "documents").mkdir()
    (storage.root / "documents" / "link").symlink_to(outside)
    with pytest.raises(StorageKeyError, match="escapes"):
        await _read_all(storage, "documents/link/secret.txt")


async def test_local_writes_are_atomic_and_leave_no_temp_files(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "root")
    key = document_object_key(uuid.uuid4(), 1, "pdf")
    await storage.put_file(key, _source(tmp_path, b"data"), content_type="application/pdf")
    files = [p.name for p in (storage.root / key).parent.iterdir()]
    assert files == ["original.pdf"]
    assert oct((storage.root / key).stat().st_mode & 0o777) == "0o640"


def test_build_storage_from_settings(tmp_path: Path) -> None:
    local = build_storage(make_settings(storage_local_root=tmp_path / "s"))
    assert isinstance(local, LocalStorage)
    s3 = build_storage(
        make_settings(storage_backend="s3", s3_bucket="b", s3_endpoint_url="http://127.0.0.1:9")
    )
    assert isinstance(s3, S3Storage)
    with pytest.raises(ValueError, match="S3_BUCKET"):
        make_settings(storage_backend="s3")
