from __future__ import annotations

import httpx
import pytest

from docintel.api.app import create_app
from docintel.db.migrations_runner import head_revisions
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


async def test_liveness(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_database_and_migrations(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["migrations"]["status"] == "ok"
    assert len(head_revisions()) == 1  # linear migration history: exactly one head


async def test_readiness_is_503_when_database_is_unreachable() -> None:
    # Port 1 on localhost refuses connections immediately.
    settings = make_settings(database_url="postgresql+psycopg://u:p@127.0.0.1:1/none")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == {
        "status": "fail",
        "detail": "database unavailable",
        "latency_ms": None,
    }
    assert "127.0.0.1" not in response.text  # no connection details leak


async def test_request_id_is_echoed_when_valid_and_replaced_when_not(
    client: httpx.AsyncClient,
) -> None:
    echoed = await client.get("/health", headers={"X-Request-ID": "trace-123.abc_DEF"})
    assert echoed.headers["X-Request-ID"] == "trace-123.abc_DEF"

    for invalid in ("has spaces in it", "x" * 65, "semi;colon"):
        replaced = await client.get("/health", headers={"X-Request-ID": invalid})
        assert replaced.headers["X-Request-ID"] != invalid
        assert len(replaced.headers["X-Request-ID"]) == 32

    generated = await client.get("/health")
    assert len(generated.headers["X-Request-ID"]) == 32


async def test_security_headers_present(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in response.headers  # only staging/production

    api_response = await client.get("/api/v1/auth/me")
    assert api_response.headers["Cache-Control"] == "no-store"


async def test_unknown_route_returns_problem_json(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == 404
    assert body["request_id"] == response.headers["X-Request-ID"]
