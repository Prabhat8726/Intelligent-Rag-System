"""Phase 8 security tests (Module 35): privilege escalation through workflows and
administration, cross-department access, malformed input, and a hijacked model trying to get
a defective invoice paid."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from docintel.synthetic.scenarios import Scenario
from tests.conftest import auth_headers
from tests.integration.conftest import Env
from tests.integration.test_agent_analysis import processed, worker
from tests.integration.test_workflows import decide, get, run, start
from tests.security.test_agent_security import HijackedLLM

pytestmark = pytest.mark.integration


async def test_nobody_escalates_through_the_admin_api(env: Env) -> None:
    for user in (env.analyst, env.reviewer, env.manager, env.viewer):
        headers = auth_headers(user)
        own = await env.client.patch(
            f"/api/v1/users/{user.id}", json={"role": "ADMIN"}, headers=headers
        )
        assert own.status_code == 403
        made = await env.client.post(
            "/api/v1/users",
            json={
                "email": f"x-{uuid.uuid4().hex[:6]}@example.test",
                "full_name": "X",
                "role": "ADMIN",
                "password": "long enough password",
            },
            headers=headers,
        )
        assert made.status_code == 403
        reset = await env.client.post(
            f"/api/v1/users/{env.admin.id}/password",
            json={"password": "long enough password"},
            headers=headers,
        )
        assert reset.status_code == 403
    # Unknown fields are rejected, not ignored (no mass assignment of e.g. password_hash).
    sneaky = await env.client.patch(
        f"/api/v1/users/{env.viewer.id}",
        json={"password_hash": "x", "failed_login_attempts": 0},
        headers=auth_headers(env.admin),
    )
    assert sneaky.status_code == 422


async def test_workflows_cannot_be_decided_across_departments_or_with_bad_input(
    env: Env, tmp_path: Path
) -> None:
    clean = await processed(env, tmp_path, Scenario.CLEAN_MATCH, 22)
    workflow = await run(env, env.analyst, clean["INV"], "INVOICE_PROCESSING")
    url = f"/api/v1/workflows/{workflow['id']}"
    # Another department's reviewer: the workflow does not exist for them.
    assert (await env.client.get(url, headers=auth_headers(env.outsider))).status_code == 404
    assert (await decide(env, env.outsider, workflow["id"], "approve")).status_code == 404
    assert (
        await env.client.post(f"{url}/cancel", json={}, headers=auth_headers(env.outsider))
    ).status_code == 404
    # Malformed input.
    assert (
        await env.client.get("/api/v1/workflows/not-a-uuid", headers=auth_headers(env.manager))
    ).status_code == 422
    for body in (
        {"reason": "x" * 1001},
        {"reason": "ok\u0000"},
        {"reason": "fine", "decided_by_id": str(env.manager.id)},
    ):
        response = await env.client.post(
            f"{url}/approve", json=body, headers=auth_headers(env.manager)
        )
        assert response.status_code == 422, body
    short = await env.client.post(
        f"{url}/reject", json={"reason": "no"}, headers=auth_headers(env.manager)
    )
    assert short.status_code == 422
    # Nothing was decided by any of it.
    assert (await get(env, workflow["id"]))["status"] == "AWAITING_APPROVAL"
    # The starter cannot cancel a workflow waiting for approval (it is decided, not withdrawn).
    waiting = await env.client.post(f"{url}/cancel", json={}, headers=auth_headers(env.analyst))
    assert waiting.status_code == 409


async def test_a_hijacked_model_cannot_get_a_defective_invoice_paid(
    env: Env, tmp_path: Path
) -> None:
    mismatch = await processed(env, tmp_path, Scenario.UNIT_PRICE_MISMATCH, 21)
    started = await start(env, env.analyst, mismatch["INV"], "INVOICE_PROCESSING")
    await worker(env, llm=HijackedLLM()).run_until_idle()
    workflow = await get(env, started["id"])
    (action,) = workflow["actions"]
    # The guardrails refused payment; the rules put the price difference to the vendor, which
    # still needs a reviewer's approval.
    assert (action["action_type"], action["proposed_by_type"], action["required_role"]) == (
        "REQUEST_VENDOR_CLARIFICATION",
        "RULES",
        "REVIEWER",
    )
    assert workflow["status"] == "AWAITING_APPROVAL"
    notes = workflow["analysis"]["recommendation"]["guardrail_notes"]
    assert any("APPROVE_FOR_PAYMENT, not allowed" in note for note in notes)
