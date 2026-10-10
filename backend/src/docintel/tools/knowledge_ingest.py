"""Load a directory of knowledge documents through the REST API (`make seed-knowledge`).

Each file is uploaded to POST /api/v1/knowledge/documents (metadata from its front matter),
then polled until the worker has processed it. Re-running is safe: files that are already in
the knowledge base are rejected by the API as duplicates and reported as such.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from docintel.tools.http import RetryAfterTransport
from docintel.tools.ingest import IngestError

KNOWLEDGE_EXTENSIONS = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
ALREADY_PRESENT = "ALREADY_PRESENT"


@dataclass(slots=True)
class KnowledgeItem:
    file: str
    knowledge_document_id: str | None = None
    status: str | None = None
    http_status: int | None = None
    title: str | None = None
    version: str | None = None
    chunks: int | None = None
    embedding_model: str | None = None
    note: str | None = None


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if response.status_code != httpx.codes.OK:
        msg = f"login failed ({response.status_code}): {response.json().get('detail')}"
        raise IngestError(msg)
    token: str = response.json()["access_token"]
    return token


def _apply(item: KnowledgeItem, body: dict[str, Any]) -> None:
    item.knowledge_document_id = body["id"]
    item.status = body["status"]
    item.title = body["title"]
    item.version = body.get("version_label")
    item.chunks = body.get("chunk_count")
    item.embedding_model = body.get("embedding_model")
    item.note = body.get("processing_error") or body.get("embedding_note")


def _discover(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in KNOWLEDGE_EXTENSIONS
    )


async def ingest_knowledge(
    directory: Path,
    *,
    api_url: str,
    email: str,
    password: str,
    timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 1.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[KnowledgeItem]:
    files = _discover(directory)
    if not files:
        msg = f"no knowledge files ({', '.join(sorted(KNOWLEDGE_EXTENSIONS))}) in {directory}"
        raise IngestError(msg)
    items = [KnowledgeItem(file=str(path.relative_to(directory))) for path in files]
    async with httpx.AsyncClient(
        base_url=api_url, timeout=60.0, transport=RetryAfterTransport(transport)
    ) as client:
        client.headers["Authorization"] = f"Bearer {await _login(client, email, password)}"
        for path, item in zip(files, items, strict=True):
            response = await client.post(
                "/api/v1/knowledge/documents",
                files={
                    "file": (
                        path.name,
                        await asyncio.to_thread(path.read_bytes),
                        KNOWLEDGE_EXTENSIONS[path.suffix.lower()],
                    )
                },
            )
            item.http_status = response.status_code
            body: dict[str, Any] = response.json()
            if response.status_code == httpx.codes.CREATED:
                _apply(item, body)
            elif response.status_code == httpx.codes.CONFLICT and "already in" in str(
                body.get("detail")
            ):
                item.status = ALREADY_PRESENT
            else:
                item.note = str(body.get("detail"))

        deadline = time.monotonic() + timeout_seconds
        pending = [item for item in items if item.status == "PROCESSING"]
        while pending and time.monotonic() < deadline:
            await asyncio.sleep(poll_interval_seconds)
            for item in pending:
                response = await client.get(
                    f"/api/v1/knowledge/documents/{item.knowledge_document_id}"
                )
                response.raise_for_status()
                _apply(item, response.json())
            pending = [item for item in pending if item.status == "PROCESSING"]
    return items
