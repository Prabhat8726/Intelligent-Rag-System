"""`make demo` (master prompt §50): the seventeen steps through the API, in process."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete

from docintel.db.models import KnowledgeDocument, User
from docintel.tools.demo import Demo, DemoError, DemoUsers
from tests.conftest import auth_headers
from tests.integration.conftest import Env
from tests.integration.test_agent_analysis import worker
from tests.integration.test_knowledge_rag import seed_knowledge_base

pytestmark = pytest.mark.integration


def token(user: User) -> str:
    return auth_headers(user)["Authorization"].removeprefix("Bearer ")


async def test_the_demonstration_path_runs_and_checks_every_step(env: Env, tmp_path: Path) -> None:
    await seed_knowledge_base(env)
    processing = worker(env)

    async def drain() -> None:  # instead of waiting for a running worker
        await processing.run_until_idle()

    lines: list[str] = []
    demo = Demo(
        env.client,
        DemoUsers(token(env.analyst), token(env.reviewer), token(env.admin)),
        web_url="http://localhost:8080",
        wait=drain,
        out=lines.append,
        timeout=60,
    )
    result = await demo.run(tmp_path / "demo", seed=4242)

    text = "\n".join(lines)
    for step in ("[1-2]", "[3]", "[4]", "[5-7]", "[8-9]", "[10]", "[11-12]", "[13]", "[14]"):
        assert step in text
    for step in ("[15]", "[16]", "[17]"):
        assert step in text
    assert "FAIL INV_PO_UNIT_PRICE" in text
    assert "REQUEST_VENDOR_CLARIFICATION" in text
    assert "maker-checker" in text
    assert "workflow.action.approved by" in text
    assert set(result.documents) == {"PO", "DN", "INV"}
    assert result.links[1] == f"http://localhost:8080/workflows/{result.workflow_id}"


async def test_a_deviation_stops_the_demonstration(env: Env, tmp_path: Path) -> None:
    """Without the knowledge base no policy is retrieved: step 10 fails, the demo stops."""
    async with env.maker() as session, session.begin():
        await session.execute(delete(KnowledgeDocument))
    processing = worker(env)

    async def drain() -> None:
        await processing.run_until_idle()

    demo = Demo(
        env.client,
        DemoUsers(token(env.analyst), token(env.reviewer), token(env.admin)),
        web_url="http://localhost:8080",
        wait=drain,
        out=lambda _: None,
        timeout=60,
    )
    with pytest.raises(DemoError, match="step 10: no policy was retrieved"):
        await demo.run(tmp_path / "demo", seed=7)


async def test_an_unreachable_stack_is_reported(tmp_path: Path) -> None:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9") as client:
        demo = Demo(client, DemoUsers("a", "b", "c"), web_url="", out=lambda _: None)
        with pytest.raises(httpx.HTTPError):
            await demo.run(tmp_path / "demo", seed=1)
