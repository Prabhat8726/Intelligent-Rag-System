"""`docintel loadtest` against the app in process: reads, processing times, NFR-09 checks."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import update

from docintel.auth.passwords import hash_password
from docintel.db.models import User
from docintel.tools.loadtest import LoadTestError, percentile, render_markdown, run_load_test
from tests.conftest import TEST_PASSWORD
from tests.factories.files import invoice_pdf_bytes
from tests.integration.conftest import Env

pytestmark = pytest.mark.integration


def test_percentile_is_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 0.5) == 51.0  # round(0.5 * 99) = 50 -> the 51st value
    assert percentile(values, 0.95) == 95.0
    assert percentile([], 0.95) == 0.0


async def _login_ready(env: Env) -> None:
    async with env.maker() as session, session.begin():
        await session.execute(
            update(User)
            .where(User.id == env.analyst.id)
            .values(password_hash=hash_password(TEST_PASSWORD))
        )


async def test_reads_and_processing_are_measured_against_the_targets(
    env: Env, tmp_path: Path
) -> None:
    await _login_ready(env)
    await env.upload(invoice_pdf_bytes())
    await env.worker().run_until_idle()
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "a.pdf").write_bytes(invoice_pdf_bytes(pages=2))

    worker = env.worker()

    class DrainingTransport(httpx.AsyncBaseTransport):
        """The app, with a worker run after each request (no worker process in tests)."""

        def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
            self.inner = inner

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await self.inner.handle_async_request(request)
            if request.method == "POST" and request.url.path == "/api/v1/documents":
                await worker.run_until_idle()
            return response

    report = await run_load_test(
        api_url="http://testserver",
        email=env.analyst.email,
        password=TEST_PASSWORD,
        users=3,
        duration_seconds=1.0,
        warmup_seconds=0.2,
        dataset=dataset,
        labels={"replicas": "in-process"},
        transport=DrainingTransport(httpx.ASGITransport(app=env.app)),
    )

    reads = report["reads"]
    assert reads["all"]["requests"] > 0
    assert set(reads) == {
        *("inbox", "document", "extraction", "findings"),
        "review_queue",
        "dashboard",
        "all",
    }
    assert report["statuses"].get("200", 0) == reads["all"]["requests"]
    assert report["transport_errors"] == {}
    processing = report["processing"]
    assert (processing["documents"], processing["native_documents"], processing["failed"]) == (
        1,
        1,
        0,
    )
    assert processing["native_document_p95_s"] > 0
    assert report["targets"]["no_failed_jobs"] == {"met": True}
    assert report["targets"]["scanned_page_p95_s"] == {
        "target": 5.0,
        "measured": None,
        "met": False,
    }
    text = render_markdown(report)
    assert "| all |" in text
    assert "replicas=in-process" in text
    assert "| scanned_page_p95_s | 5.0 | not measured | no |" in text


async def test_an_empty_stack_is_refused(env: Env) -> None:
    await _login_ready(env)
    with pytest.raises(LoadTestError, match="no processed document"):
        await run_load_test(
            api_url="http://testserver",
            email=env.analyst.email,
            password=TEST_PASSWORD,
            users=1,
            duration_seconds=0.1,
            transport=httpx.ASGITransport(app=env.app),
        )
