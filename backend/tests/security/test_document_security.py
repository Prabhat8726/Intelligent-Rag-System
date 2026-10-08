"""Upload and document-access security (docs/architecture/09-security-architecture.md §3)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.db.models import AuditLog, Department, DocumentVersion, Role, User
from tests.conftest import ClientFactory, auth_headers, make_user
from tests.factories.files import image_bytes, pdf_bytes

pytestmark = pytest.mark.integration

PDF = pdf_bytes()


async def _upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    content: bytes = PDF,
    filename: str = "invoice.pdf",
    content_type: str = "application/pdf",
) -> httpx.Response:
    return await client.post(
        "/api/v1/documents", headers=headers, files={"file": (filename, content, content_type)}
    )


@pytest.fixture
async def finance_analyst(db_session: AsyncSession, department: Department) -> User:
    return await make_user(db_session, role=Role.ANALYST, department=department)


@pytest.fixture
async def finance_document(client: httpx.AsyncClient, finance_analyst: User) -> str:
    response = await _upload(client, auth_headers(finance_analyst))
    assert response.status_code == 201, response.text
    document_id: str = response.json()["id"]
    return document_id


# ------------------------------------------------------------------------------ authn / authz
async def test_anonymous_requests_are_rejected(client: httpx.AsyncClient) -> None:
    assert (await _upload(client, {})).status_code == 401
    assert (await client.get("/api/v1/documents")).status_code == 401


async def test_viewer_cannot_upload_and_denial_is_audited(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    viewer = await make_user(db_session, role=Role.VIEWER, department=department)
    response = await _upload(client, auth_headers(viewer))
    assert response.status_code == 403
    denial = await db_session.scalar(
        select(AuditLog).where(AuditLog.actor_id == viewer.id, AuditLog.action == "authz.denied")
    )
    assert denial is not None
    assert denial.details == {"permission": "documents:upload"}


async def test_other_departments_cannot_see_or_touch_documents(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    other_department: Department,
    finance_document: str,
) -> None:
    outsider_analyst = await make_user(db_session, role=Role.ANALYST, department=other_department)
    outsider_manager = await make_user(db_session, role=Role.MANAGER, department=other_department)
    base = f"/api/v1/documents/{finance_document}"

    for user in (outsider_analyst, outsider_manager):
        headers = auth_headers(user)
        assert (await client.get(base, headers=headers)).status_code == 404
        assert (await client.get(f"{base}/file", headers=headers)).status_code == 404
        assert (await client.post(f"{base}/process", headers=headers)).status_code == 404
        listing = (await client.get("/api/v1/documents", headers=headers)).json()
        assert listing["total"] == 0
    assert (await client.delete(base, headers=auth_headers(outsider_manager))).status_code == 404


async def test_same_department_and_admin_can_read(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    department: Department,
    finance_document: str,
) -> None:
    colleague = await make_user(db_session, role=Role.VIEWER, department=department)
    admin = await make_user(db_session, role=Role.ADMIN)
    for user in (colleague, admin):
        response = await client.get(
            f"/api/v1/documents/{finance_document}", headers=auth_headers(user)
        )
        assert response.status_code == 200


async def test_duplicate_detection_does_not_leak_other_departments(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    other_department: Department,
    finance_document: str,
) -> None:
    outsider = await make_user(db_session, role=Role.ANALYST, department=other_department)
    same_bytes = await _upload(client, auth_headers(outsider))
    assert same_bytes.status_code == 201
    assert same_bytes.json()["duplicate_of_id"] is None


async def test_unknown_ids_and_malformed_ids(
    client: httpx.AsyncClient, finance_analyst: User
) -> None:
    headers = auth_headers(finance_analyst)
    assert (
        await client.get(f"/api/v1/documents/{uuid.uuid4()}", headers=headers)
    ).status_code == 404
    assert (await client.get("/api/v1/documents/not-a-uuid", headers=headers)).status_code == 422


# ------------------------------------------------------------------------------ upload limits
async def test_declared_oversized_body_is_rejected_before_parsing(
    client_factory: ClientFactory, finance_analyst: User, storage_root: Path
) -> None:
    small = await client_factory(upload_max_bytes=64 * 1024)
    big = pdf_bytes() + b"%" + b"x" * (2 * 1024 * 1024)
    response = await _upload(small, auth_headers(finance_analyst), content=big)
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    # Rejected by the ASGI body limit (declared Content-Length), not after spooling.
    assert "request body exceeds" in response.json()["detail"]
    assert not (storage_root / "documents").exists()


async def test_streamed_oversized_body_without_length_is_cut_off(
    client_factory: ClientFactory, finance_analyst: User
) -> None:
    small = await client_factory(upload_max_bytes=64 * 1024)
    boundary = "docintel-boundary"

    async def body() -> AsyncIterator[bytes]:
        yield (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="big.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF-1.7\n'
        ).encode()
        for _ in range(40):
            yield b"x" * 65536  # 2.5 MiB total, never announced in Content-Length
        yield f"\r\n--{boundary}--\r\n".encode()

    response = await small.post(
        "/api/v1/documents",
        headers={
            **auth_headers(finance_analyst),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        content=body(),
    )
    assert response.status_code == 413
    assert "request body exceeds" in response.json()["detail"]


async def test_json_endpoints_have_a_small_body_limit(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        content=b'{"email": "a@b.c", "password": "' + b"x" * (2 * 1024 * 1024) + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


# ------------------------------------------------------------------------------ content checks
@pytest.mark.parametrize(
    ("content", "filename", "content_type", "expected"),
    [
        (b"<html><script>alert(1)</script></html>", "invoice.pdf", "application/pdf", 415),
        (image_bytes("PNG"), "invoice.pdf", "application/pdf", 415),
        (b"MZ\x90\x00", "payload.exe", "application/octet-stream", 415),
        (PDF, "invoice.pdf", "text/html", 415),
        (pdf_bytes(user_password="locked"), "locked.pdf", "application/pdf", 422),
        (b"%PDF-1.7 not really a pdf", "broken.pdf", "application/pdf", 422),
        (b"", "empty.pdf", "application/pdf", 422),
    ],
)
async def test_malicious_or_invalid_files_are_rejected_and_nothing_is_stored(
    client: httpx.AsyncClient,
    finance_analyst: User,
    storage_root: Path,
    content: bytes,
    filename: str,
    content_type: str,
    expected: int,
) -> None:
    response = await _upload(
        client,
        auth_headers(finance_analyst),
        content=content,
        filename=filename,
        content_type=content_type,
    )
    assert response.status_code == expected
    assert response.headers["content-type"] == "application/problem+json"
    assert not list(storage_root.rglob("original.*"))


async def test_path_traversal_filename_cannot_influence_storage_location(
    client: httpx.AsyncClient, db_session: AsyncSession, finance_analyst: User, storage_root: Path
) -> None:
    response = await _upload(
        client, auth_headers(finance_analyst), filename="../../../../etc/cron.d/evil.pdf"
    )
    assert response.status_code == 201
    body = response.json()
    assert body["display_filename"] == "evil.pdf"
    version = await db_session.get(DocumentVersion, uuid.UUID(body["current_version"]["id"]))
    assert version is not None
    assert version.storage_key == f"documents/{body['id']}/v1/original.pdf"
    stored = list(storage_root.rglob("original.pdf"))
    assert len(stored) == 1
    assert stored[0].resolve().is_relative_to(storage_root.resolve())


async def test_invalid_form_values_are_rejected(
    client: httpx.AsyncClient, finance_analyst: User
) -> None:
    response = await client.post(
        "/api/v1/documents",
        headers=auth_headers(finance_analyst),
        files={"file": ("invoice.pdf", PDF, "application/pdf")},
        data={"sensitivity": "TOP_SECRET_ULTRA"},
    )
    assert response.status_code == 422


async def test_missing_blob_yields_404_not_500(
    client: httpx.AsyncClient, finance_analyst: User, finance_document: str, storage_root: Path
) -> None:
    for path in storage_root.rglob("original.pdf"):
        path.unlink()
    response = await client.get(
        f"/api/v1/documents/{finance_document}/file", headers=auth_headers(finance_analyst)
    )
    assert response.status_code == 404
