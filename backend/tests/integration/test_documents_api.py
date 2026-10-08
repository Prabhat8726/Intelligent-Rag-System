from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.db.models import (
    AuditLog,
    Department,
    Document,
    JobStatus,
    ProcessingJob,
    Role,
    User,
)
from tests.conftest import auth_headers, make_user
from tests.factories.files import image_bytes, pdf_bytes

pytestmark = pytest.mark.integration

PDF = pdf_bytes(pages=2)


async def _upload(
    client: httpx.AsyncClient,
    user: User,
    content: bytes = PDF,
    filename: str = "invoice-1001.pdf",
    content_type: str = "application/pdf",
    data: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        "/api/v1/documents",
        headers=auth_headers(user),
        files={"file": (filename, content, content_type)},
        data=data or {},
    )


async def _audit_actions(session: AsyncSession, entity_id: str) -> list[str]:
    rows = await session.scalars(
        select(AuditLog.action).where(AuditLog.entity_id == entity_id).order_by(AuditLog.id)
    )
    return list(rows)


@pytest.fixture
async def analyst(db_session: AsyncSession, department: Department) -> User:
    return await make_user(db_session, role=Role.ANALYST, department=department)


async def test_upload_stores_file_and_queues_processing(
    client: httpx.AsyncClient, db_session: AsyncSession, analyst: User, storage_root: Path
) -> None:
    response = await _upload(client, analyst)

    assert response.status_code == 201, response.text
    body = response.json()
    assert response.headers["Location"] == f"/api/v1/documents/{body['id']}"
    assert body["status"] == "PENDING"
    assert body["sensitivity"] == "INTERNAL"
    assert body["source"] == "UPLOAD"
    assert body["display_filename"] == "invoice-1001.pdf"
    assert body["owner"]["id"] == str(analyst.id)
    assert body["department"]["id"] == str(analyst.department_id)
    assert body["duplicate_of_id"] is None
    version = body["current_version"]
    assert version["version_number"] == 1
    assert version["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert version["size_bytes"] == len(PDF)
    assert version["page_count"] == 2
    assert version["mime_type"] == "application/pdf"

    stored = storage_root / "documents" / body["id"] / "v1" / "original.pdf"
    assert stored.read_bytes() == PDF

    job = await db_session.scalar(
        select(ProcessingJob).where(ProcessingJob.document_id == uuid.UUID(body["id"]))
    )
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.document_version_id == uuid.UUID(version["id"])
    assert job.requested_by_id == analyst.id
    assert await _audit_actions(db_session, body["id"]) == ["document.uploaded"]


async def test_upload_accepts_images_and_sensitivity(
    client: httpx.AsyncClient, analyst: User
) -> None:
    response = await _upload(
        client,
        analyst,
        content=image_bytes("TIFF", frames=2),
        filename="fax.tif",
        content_type="image/tiff",
        data={"sensitivity": "CONFIDENTIAL"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["sensitivity"] == "CONFIDENTIAL"
    assert body["current_version"]["page_count"] == 2
    assert body["current_version"]["mime_type"] == "image/tiff"


async def test_exact_duplicate_is_accepted_and_flagged(
    client: httpx.AsyncClient, analyst: User
) -> None:
    first = (await _upload(client, analyst)).json()
    second = (await _upload(client, analyst, filename="resent.pdf")).json()
    assert second["id"] != first["id"]
    assert second["duplicate_of_id"] == first["id"]
    assert second["duplicate_reason"] == "EXACT_FILE_HASH"


async def test_list_filters_and_paginates(
    client: httpx.AsyncClient, db_session: AsyncSession, analyst: User, department: Department
) -> None:
    colleague = await make_user(db_session, role=Role.ANALYST, department=department)
    for index in range(3):
        await _upload(
            client, analyst, content=pdf_bytes(text=f"A{index}"), filename=f"po-{index}.pdf"
        )
    await _upload(client, colleague, content=pdf_bytes(text="C"), filename="colleague_invoice.pdf")

    everything = (await client.get("/api/v1/documents", headers=auth_headers(analyst))).json()
    assert everything["total"] == 4  # department colleagues' documents are visible

    mine = (
        await client.get(
            "/api/v1/documents", params={"mine": "true"}, headers=auth_headers(analyst)
        )
    ).json()
    assert mine["total"] == 3

    page = (
        await client.get(
            "/api/v1/documents", params={"limit": 2, "offset": 2}, headers=auth_headers(analyst)
        )
    ).json()
    assert (page["total"], len(page["items"]), page["offset"]) == (4, 2, 2)

    search = (
        await client.get(
            "/api/v1/documents", params={"q": "colleague_"}, headers=auth_headers(analyst)
        )
    ).json()
    assert [item["display_filename"] for item in search["items"]] == ["colleague_invoice.pdf"]

    # LIKE wildcards in the query are matched literally.
    wildcard = (
        await client.get("/api/v1/documents", params={"q": "%"}, headers=auth_headers(analyst))
    ).json()
    assert wildcard["total"] == 0

    pending = (
        await client.get(
            "/api/v1/documents", params={"status": "PENDING"}, headers=auth_headers(analyst)
        )
    ).json()
    assert pending["total"] == 4


async def test_detail_includes_latest_job(client: httpx.AsyncClient, analyst: User) -> None:
    created = (await _upload(client, analyst)).json()
    detail = await client.get(f"/api/v1/documents/{created['id']}", headers=auth_headers(analyst))
    assert detail.status_code == 200
    body = detail.json()
    assert body["latest_job"]["status"] == "QUEUED"
    assert body["latest_job"]["attempts"] == 0
    assert body["latest_job"]["duration_ms"] is None
    assert body["inspection"] is None


async def test_download_streams_original_with_safe_headers(
    client: httpx.AsyncClient, db_session: AsyncSession, analyst: User
) -> None:
    created = (await _upload(client, analyst, filename="Rechnung März.pdf")).json()
    response = await client.get(
        f"/api/v1/documents/{created['id']}/file", headers=auth_headers(analyst)
    )
    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["content-type"] == "application/pdf"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="Rechnung M_rz.pdf"')
    assert f"filename*=UTF-8''{quote('Rechnung März.pdf', safe='')}" in disposition
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert "document.downloaded" in await _audit_actions(db_session, created["id"])


async def test_manager_soft_deletes_and_queued_jobs_are_cancelled(
    client: httpx.AsyncClient, db_session: AsyncSession, analyst: User, department: Department
) -> None:
    manager = await make_user(db_session, role=Role.MANAGER, department=department)
    created = (await _upload(client, analyst)).json()

    assert (
        await client.delete(f"/api/v1/documents/{created['id']}", headers=auth_headers(analyst))
    ).status_code == 403
    deleted = await client.delete(
        f"/api/v1/documents/{created['id']}", headers=auth_headers(manager)
    )
    assert deleted.status_code == 204

    assert (
        await client.get(f"/api/v1/documents/{created['id']}", headers=auth_headers(manager))
    ).status_code == 404
    listing = (await client.get("/api/v1/documents", headers=auth_headers(manager))).json()
    assert created["id"] not in [item["id"] for item in listing["items"]]

    document = await db_session.get(Document, uuid.UUID(created["id"]))
    assert document is not None
    assert document.deleted_by_id == manager.id
    job = await db_session.scalar(
        select(ProcessingJob).where(ProcessingJob.document_id == document.id)
    )
    assert job is not None
    assert job.status == JobStatus.CANCELLED
    assert (await _audit_actions(db_session, created["id"]))[-1] == "document.deleted"


async def test_reprocess_conflicts_while_active_then_queues(
    client: httpx.AsyncClient, db_session: AsyncSession, analyst: User
) -> None:
    created = (await _upload(client, analyst)).json()
    url = f"/api/v1/documents/{created['id']}/process"

    conflict = await client.post(url, headers=auth_headers(analyst))
    assert conflict.status_code == 409

    job = await db_session.scalar(
        select(ProcessingJob).where(ProcessingJob.document_id == uuid.UUID(created["id"]))
    )
    assert job is not None
    job.status = JobStatus.COMPLETED
    await db_session.commit()

    accepted = await client.post(url, headers=auth_headers(analyst))
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["status"] == "QUEUED"
    assert accepted.json()["id"] != str(job.id)
    detail = (
        await client.get(f"/api/v1/documents/{created['id']}", headers=auth_headers(analyst))
    ).json()
    assert detail["status"] == "PENDING"
    assert detail["latest_job"]["id"] == accepted.json()["id"]


async def test_admin_can_file_into_a_department_but_analyst_cannot(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    analyst: User,
    other_department: Department,
) -> None:
    admin = await make_user(db_session, role=Role.ADMIN)
    filed = await _upload(client, admin, data={"department_id": str(other_department.id)})
    assert filed.status_code == 201
    assert filed.json()["department"]["id"] == str(other_department.id)

    refused = await _upload(client, analyst, data={"department_id": str(other_department.id)})
    assert refused.status_code == 403

    missing = await _upload(client, admin, data={"department_id": str(uuid.uuid4())})
    assert missing.status_code == 422


async def test_blob_is_removed_when_the_database_write_fails(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    analyst: User,
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _failing_commit() -> None:
        msg = "simulated database outage"
        raise RuntimeError(msg)

    monkeypatch.setattr(db_session, "commit", _failing_commit)
    response = await _upload(client, analyst)
    assert response.status_code == 500
    assert list((storage_root / "documents").rglob("*.pdf")) == []
