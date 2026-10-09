"""MCP (Module 16): personal API tokens and the tool server over an in-process connection and
real streamable HTTP - same tools, permissions, scoping and audit as the web UI."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select

from docintel.agent.mcp_server import build_server, http_app, stdio_identity
from docintel.agent.tools import ToolEnvironment, build_registry
from docintel.auth.api_tokens import hash_token
from docintel.db.models import AgentToolCall, ApiToken, AuditLog, ToolChannel, User
from docintel.synthetic.scenarios import Scenario
from tests.conftest import auth_headers
from tests.integration.conftest import Env
from tests.integration.test_matching_api import bundle

pytestmark = pytest.mark.integration

READ_SCOPES = ["documents:read", "knowledge:read"]
READ_TOOLS = {
    "search_documents",
    "get_document",
    "get_extracted_fields",
    "get_document_evidence",
    "search_knowledge_base",
    "run_business_rules",
}


async def create_token(env: Env, user: User, scopes: list[str], **extra: Any) -> httpx.Response:
    return await env.client.post(
        "/api/v1/auth/tokens",
        json={"name": "Laptop client", "scopes": scopes, **extra},
        headers=auth_headers(user),
    )


async def invoice(env: Env, tmp_path: Path) -> str:
    docs = bundle(tmp_path, Scenario.CLEAN_MATCH, seed=22)
    document_id = await env.upload(docs["INV"][0], "invoice.pdf")
    assert await env.worker().run_until_idle() == 1
    return document_id


def registry(env: Env) -> Any:
    return build_registry(ToolEnvironment(env.settings, env.maker, env.app.state.rag))


async def test_api_tokens_are_shown_once_scoped_and_revocable(env: Env) -> None:
    created = await create_token(env, env.reviewer, READ_SCOPES, expires_in_days=7)
    assert created.status_code == 201, created.text
    body = created.json()
    secret = body["token"]
    assert secret.startswith("dit_")
    assert len(secret) > 40
    assert body["prefix"] == secret[:12]
    assert body["scopes"] == READ_SCOPES

    listed = await env.client.get("/api/v1/auth/tokens", headers=auth_headers(env.reviewer))
    assert listed.status_code == 200
    (item,) = listed.json()
    assert "token" not in item
    assert item["prefix"] == secret[:12]
    async with env.maker() as session:
        stored = await session.scalar(select(ApiToken).where(ApiToken.id == item["id"]))
        assert stored is not None
        assert stored.token_hash == hash_token(secret)  # only the hash is kept
        assert secret not in str([row.details for row in await session.scalars(select(AuditLog))])

    # Scopes never exceed the owner's permissions or the tool permissions; expiry is capped.
    for user, scopes, extra, message in (
        (
            env.viewer,
            ["comparisons:create"],
            {},
            "Scopes not available to you: comparisons:create.",
        ),
        (env.admin, ["users:manage"], {}, "Scopes not available to you: users:manage."),
        (env.reviewer, READ_SCOPES, {"expires_in_days": 200}, "Tokens expire after at most 90"),
    ):
        refused = await create_token(env, user, scopes, **extra)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"].startswith(message)

    # Another user cannot revoke it; its owner can.
    other = await env.client.delete(
        f"/api/v1/auth/tokens/{item['id']}", headers=auth_headers(env.analyst)
    )
    assert other.status_code == 404
    revoked = await env.client.delete(
        f"/api/v1/auth/tokens/{item['id']}", headers=auth_headers(env.reviewer)
    )
    assert revoked.status_code == 204


async def test_mcp_tools_run_as_the_token_owner(env: Env, tmp_path: Path) -> None:
    document_id = await invoice(env, tmp_path)
    secret = (await create_token(env, env.reviewer, READ_SCOPES)).json()["token"]
    server = build_server(registry(env), stdio_identity(env.maker, env.settings, secret))

    async with Client(server) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == READ_TOOLS
        search = next(tool for tool in listed.tools if tool.name == "search_documents")
        assert search.input_schema["additionalProperties"] is False
        assert search.output_schema is not None
        assert search.annotations is not None
        assert search.annotations.read_only_hint is True

        found = await client.call_tool("get_document", {"document_id": document_id})
        assert found.is_error is False
        assert found.structured_content["filename"] == "invoice.pdf"

        # Outside the token's scopes, even though the reviewer's role could compare.
        denied = await client.call_tool(
            "compare_documents",
            {"documents": [{"document_id": document_id, "role": "INVOICE"}] * 2},
        )
        assert denied.is_error is True
        assert denied.content[0].text.startswith("DENIED: Not permitted")  # type: ignore[union-attr]
        invalid = await client.call_tool("get_document", {"document_id": "1; DROP TABLE x"})
        assert invalid.is_error is True
        assert invalid.content[0].text.startswith("INVALID: document_id")  # type: ignore[union-attr]

        # Revocation applies to the next call of an open session.
        async with env.maker() as session, session.begin():
            token = await session.scalar(
                select(ApiToken).where(ApiToken.user_id == env.reviewer.id)
            )
            assert token is not None
            token.revoked_at = func.now()
        gone = await client.call_tool("get_document", {"document_id": document_id})
        assert gone.is_error is True
        assert gone.content[0].text == "Authentication required."  # type: ignore[union-attr]
        assert (await client.list_tools()).tools == []

    async with env.maker() as session:
        calls = list(
            await session.scalars(
                select(AgentToolCall)
                .where(AgentToolCall.actor_id == env.reviewer.id)
                .order_by(AgentToolCall.created_at)
            )
        )
        failures = await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "mcp.auth_failed")
        )
    assert [(call.tool_name, call.status.value) for call in calls] == [
        ("get_document", "SUCCEEDED"),
        ("compare_documents", "DENIED"),
        ("get_document", "INVALID"),
    ]
    assert all(call.via == ToolChannel.MCP for call in calls)
    assert failures == 2


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextlib.asynccontextmanager
async def serving(env: Env) -> AsyncIterator[str]:
    port = free_port()
    app = http_app(registry(env), env.maker, env.settings, host="127.0.0.1", port=port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


async def test_mcp_over_streamable_http_requires_a_bearer_token(env: Env, tmp_path: Path) -> None:
    document_id = await invoice(env, tmp_path)
    secret = (await create_token(env, env.analyst, ["documents:read"])).json()["token"]
    async with serving(env) as url:
        async with httpx.AsyncClient() as raw:
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            anonymous = await raw.post(url, json=payload)
            assert anonymous.status_code == 401
            assert anonymous.headers["www-authenticate"].startswith("Bearer")
            forged = await raw.post(
                url, json=payload, headers={"Authorization": "Bearer dit_not-a-real-token"}
            )
            assert forged.status_code == 401
            rebinding = await raw.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {secret}", "Host": "attacker.example"},
            )
            assert rebinding.status_code in (403, 421)

        headers = {"Authorization": f"Bearer {secret}"}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=30) as http,
            Client(streamable_http_client(url, http_client=http)) as client,
        ):
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == READ_TOOLS - {"search_knowledge_base"}
            result = await client.call_tool(
                "get_extracted_fields",
                {"document_id": document_id, "field_paths": ["total"]},
            )
            assert result.is_error is False
            (field,) = result.structured_content["fields"]
            assert field["field_path"] == "total"
