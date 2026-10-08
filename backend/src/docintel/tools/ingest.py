"""Bulk ingestion through the public REST API (`make process`).

Uses only the HTTP API - exactly what an external integration would do - so it exercises
authentication, validation, storage, the queue and the worker end to end.
Writes `ingest-report.json` mapping each file (and synthetic doc_id) to its platform document id.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from docintel.documents.validation import SUPPORTED_EXTENSIONS

TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "REVIEW_REQUIRED"})
REPORT_NAME = "ingest-report.json"
_MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


class IngestError(Exception):
    pass


@dataclass(slots=True)
class IngestItem:
    file: str
    doc_id: str | None
    document_id: str | None = None
    status: str | None = None
    http_status: int | None = None
    error: str | None = None
    page_count: int | None = None
    inspection_kind: str | None = None
    pages_needing_ocr: list[int] | None = None
    duplicate_of_id: str | None = None
    processing_ms: int | None = None
    document_type: str | None = None
    type_confidence: str | None = None
    review_reasons: list[str] | None = None
    variant: str | None = None  # native | scanned (from the dataset manifest)
    extraction_status: str | None = None
    extraction_confidence: str | None = None
    review_level: str | None = None
    vendor: str | None = None


def _discover(directory: Path) -> list[IngestItem]:
    manifest = directory / "manifest.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return [
            IngestItem(file=entry["file"], doc_id=entry["doc_id"], variant=entry.get("variant"))
            for entry in data["documents"]
        ]
    return [
        IngestItem(file=str(path.relative_to(directory)), doc_id=None)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    ]


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if response.status_code != httpx.codes.OK:
        msg = f"login failed ({response.status_code}): {response.json().get('detail')}"
        raise IngestError(msg)
    token: str = response.json()["access_token"]
    return token


async def _upload(client: httpx.AsyncClient, directory: Path, item: IngestItem) -> None:
    path = directory / item.file
    mime = _MIME_BY_EXTENSION.get(path.suffix.lower(), "application/octet-stream")
    response = await client.post(
        "/api/v1/documents", files={"file": (path.name, path.read_bytes(), mime)}
    )
    item.http_status = response.status_code
    body: dict[str, Any] = response.json()
    if response.status_code == httpx.codes.CREATED:
        item.document_id = body["id"]
        item.status = body["status"]
        item.page_count = body["current_version"]["page_count"]
        item.duplicate_of_id = body["duplicate_of_id"]
    else:
        item.error = str(body.get("detail"))


async def _refresh(client: httpx.AsyncClient, item: IngestItem) -> None:
    response = await client.get(f"/api/v1/documents/{item.document_id}")
    response.raise_for_status()
    body = response.json()
    item.status = body["status"]
    item.duplicate_of_id = body["duplicate_of_id"]
    item.document_type = body.get("document_type")
    item.type_confidence = body.get("type_confidence")
    item.review_reasons = body.get("review_reasons") or []
    if body.get("inspection"):
        item.inspection_kind = body["inspection"]["kind"]
        item.pages_needing_ocr = body["inspection"]["pages_needing_ocr"]
    item.vendor = (body.get("vendor") or {}).get("canonical_name")
    job = body.get("latest_job")
    if job:
        item.processing_ms = job.get("duration_ms")
        if item.status == "FAILED":
            item.error = body.get("processing_error") or job.get("last_error")
    if item.status in ("COMPLETED", "REVIEW_REQUIRED"):
        extraction = await client.get(f"/api/v1/documents/{item.document_id}/extraction")
        if extraction.status_code == httpx.codes.OK:
            data = extraction.json()
            item.extraction_status = data["status"]
            item.extraction_confidence = data["overall_confidence"]
            item.review_level = data["review_level"]


async def ingest_directory(
    directory: Path,
    *,
    api_url: str,
    email: str,
    password: str,
    timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 1.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[IngestItem]:
    items = _discover(directory)
    if not items:
        msg = f"no supported files found in {directory}"
        raise IngestError(msg)
    async with httpx.AsyncClient(base_url=api_url, timeout=60.0, transport=transport) as client:
        client.headers["Authorization"] = f"Bearer {await _login(client, email, password)}"
        for item in items:
            await _upload(client, directory, item)

        deadline = time.monotonic() + timeout_seconds
        pending = [item for item in items if item.document_id]
        while pending and time.monotonic() < deadline:
            for item in pending:
                await _refresh(client, item)
            pending = [item for item in pending if item.status not in TERMINAL_STATUSES]
            if pending:
                await asyncio.sleep(poll_interval_seconds)

    (directory / REPORT_NAME).write_text(
        json.dumps({"api_url": api_url, "items": [asdict(i) for i in items]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return items


def summarize(items: list[IngestItem]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in items:
        key = item.status or f"REJECTED_{item.http_status}"
        summary[key] = summary.get(key, 0) + 1
    return summary
