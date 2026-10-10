import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { saveSession } from "../auth/session";
import type { AuditEventPage, Report, UserPage, Workflow, WorkflowAction } from "../lib/types";
import { CURRENT_USER, jsonResponse, mockFetch, problem, renderApp } from "../test/utils";

const DOC_ID = "5b0d8a3e-6b43-4c86-9f73-0b5b0f1f0a11";
const WF_ID = "7a6b5c4d-0000-4000-8000-0000000000cc";
const ACTION_ID = "3c2b1a00-0000-4000-8000-0000000000dd";
const REPORT_ID = "2d3e4f50-0000-4000-8000-0000000000ee";
const ANALYST = { id: "a1", email: "analyst@docintel.local", full_name: "Finance Analyst" };
const MANAGER = { id: "m1", email: "manager@docintel.local", full_name: "Finance Manager" };
// Role permissions as in backend/src/docintel/auth/permissions.py (the parts these screens use).
const VIEWER = ["documents:read", "workflows:read", "reports:read", "analysis:read"];
const MANAGER_PERMISSIONS = [...VIEWER, "workflows:start", "workflows:approve", "reports:create", "audit:read"];
const ADMIN = [...MANAGER_PERMISSIONS, "users:manage"];

function me(permissions: string[], overrides: Record<string, unknown> = {}) {
  saveSession({ token: "header.payload.signature", expiresAt: Date.now() + 60_000 });
  return () => jsonResponse({ ...CURRENT_USER, ...overrides, permissions });
}

function action(overrides: Partial<WorkflowAction> = {}): WorkflowAction {
  return {
    id: ACTION_ID,
    action_type: "APPROVE_FOR_PAYMENT",
    title: "Approve for payment",
    status: "AWAITING_APPROVAL",
    risk_level: "HIGH",
    requires_approval: true,
    required_role: "MANAGER",
    proposed_by_type: "RULES",
    rationale: "Every applicable rule passed and the evidence is strong; payment needs approval.",
    confidence_level: "HIGH",
    confidence_score: 0.95,
    payload: {
      document: { document_number: "INV-1001", vendor_name: "Kestrel", total: "962.55", currency: "USD" },
      issues: [],
    },
    decided_by: null,
    decided_at: null,
    decision_reason: null,
    executed_at: null,
    execution_result: null,
    error: null,
    created_at: "2026-10-10T10:00:00Z",
    transitions: [
      {
        from_status: null, to_status: "PROPOSED", actor_type: "SYSTEM", actor: ANALYST, reason: "Every rule passed.",
        created_at: "2026-10-10T10:00:00Z",
      },
      {
        from_status: "PROPOSED", to_status: "AWAITING_APPROVAL", actor_type: "SYSTEM", actor: null,
        reason: "Risk HIGH: needs a MANAGER.", created_at: "2026-10-10T10:00:01Z",
      },
    ],
    can_decide: true,
    blockers: [],
    ...overrides,
  };
}

function workflow(overrides: Partial<Workflow> = {}): Workflow {
  const pending = action();
  return {
    id: WF_ID,
    workflow_type: "INVOICE_PROCESSING",
    title: "Invoice processing",
    status: "AWAITING_APPROVAL",
    outcome: null,
    trigger: "MANUAL",
    document: {
      id: DOC_ID, filename: "invoice-1001.pdf", document_type: "INVOICE", status: "COMPLETED", version_number: 1,
      is_current_version: true,
    },
    initiated_by: ANALYST,
    current_step: "approval",
    pending_action: {
      id: ACTION_ID, action_type: "APPROVE_FOR_PAYMENT", title: "Approve for payment", risk_level: "HIGH",
      required_role: "MANAGER",
    },
    error: null,
    created_at: "2026-10-10T10:00:00Z",
    started_at: "2026-10-10T10:00:00Z",
    finished_at: null,
    definition_version: 1,
    steps: [
      { sequence: 1, step_name: "check_document", status: "COMPLETED", output: {}, error: null, started_at: null, finished_at: null },
      { sequence: 2, step_name: "investigate", status: "COMPLETED", output: {}, error: null, started_at: null, finished_at: null },
      { sequence: 3, step_name: "propose_action", status: "COMPLETED", output: {}, error: null, started_at: null, finished_at: null },
      { sequence: 4, step_name: "approval", status: "RUNNING", output: {}, error: null, started_at: null, finished_at: null },
      { sequence: 5, step_name: "execute_action", status: "PENDING", output: {}, error: null, started_at: null, finished_at: null },
      { sequence: 6, step_name: "report", status: "PENDING", output: {}, error: null, started_at: null, finished_at: null },
    ],
    actions: [pending],
    agent_run_id: "r1",
    analysis: null,
    report_ids: [],
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Workflows", () => {
  it("lists the workflows awaiting my decision and counts them in the navigation", async () => {
    const { pending_action, ...summary } = workflow();
    mockFetch({
      "/api/v1/auth/me": me(MANAGER_PERMISSIONS, { role: "MANAGER" }),
      "/api/v1/workflows/summary": () => jsonResponse({ awaiting_my_decision: 1 }),
      "/api/v1/workflows?limit=50&awaiting_me=true": () =>
        jsonResponse({ items: [{ ...summary, pending_action }], total: 1, limit: 50, offset: 0 }),
    });
    renderApp("/workflows");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(await within(nav).findByLabelText("1 awaiting your decision")).toBeInTheDocument();
    const settings = screen.getByRole("navigation", { name: "Settings" });
    expect(within(settings).getByRole("link", { name: "Audit log" })).toBeInTheDocument();
    expect(within(settings).queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    const list = await screen.findByRole("region", { name: "Workflow list" });
    expect(within(list).getByRole("link", { name: "invoice-1001.pdf" })).toHaveAttribute("href", `/workflows/${WF_ID}`);
    expect(within(list).getByText("Approve for payment (manager)")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Awaiting my decision" })).toHaveAttribute("aria-selected", "true");
  });

  it("approves a proposed payment and shows the result and its history", async () => {
    let body: unknown = null;
    const decided = workflow({
      status: "COMPLETED",
      outcome: "APPROVED_FOR_PAYMENT",
      pending_action: null,
      report_ids: [REPORT_ID],
      actions: [
        action({
          status: "EXECUTED",
          can_decide: false,
          decided_by: MANAGER,
          decided_at: "2026-10-10T10:05:00Z",
          decision_reason: "Checked against the PO.",
          executed_at: "2026-10-10T10:05:00Z",
          execution_result: {
            decision: "APPROVED_FOR_PAYMENT",
            payment_reference: "PAY-3C2B1A0000",
            note: "Released for the next payment run.",
          },
        }),
      ],
    });
    mockFetch({
      "/api/v1/auth/me": me(MANAGER_PERMISSIONS, { role: "MANAGER" }),
      "/api/v1/workflows/summary": () => jsonResponse({ awaiting_my_decision: 1 }),
      [`/api/v1/workflows/${WF_ID}`]: () => jsonResponse(workflow()),
      [`/api/v1/workflows/${WF_ID}/approve`]: (_url, init) => {
        body = JSON.parse(init?.body as string);
        return jsonResponse(decided);
      },
    });
    renderApp(`/workflows/${WF_ID}`);
    const card = await screen.findByRole("region", { name: "Proposed action" });
    expect(within(card).getByText(/INV-1001 · Kestrel · 962.55 USD/)).toBeInTheDocument();
    const form = within(card).getByRole("form", { name: "Decision" });
    expect(within(form).getByRole("button", { name: "Reject" })).toBeDisabled(); // a reason is required
    await userEvent.type(within(form).getByLabelText(/Reason/), "Checked against the PO.");
    await userEvent.click(within(form).getByRole("button", { name: "Approve" }));
    await waitFor(() => {
      expect(body).toEqual({ reason: "Checked against the PO." });
    });
    const result = await screen.findByLabelText("Result");
    expect(within(result).getByText("Payment reference: PAY-3C2B1A0000")).toBeInTheDocument();
    expect(screen.getByText("Approved", { exact: true }).closest("p")).toHaveTextContent("Finance Manager");
    expect(screen.getByRole("link", { name: "Open the report" })).toHaveAttribute("href", `/reports/${REPORT_ID}`);
    expect(screen.queryByRole("form", { name: "Decision" })).not.toBeInTheDocument();
  });

  it("explains why the starter cannot approve their own proposal", async () => {
    mockFetch({
      "/api/v1/auth/me": me(MANAGER_PERMISSIONS, { role: "MANAGER" }),
      "/api/v1/workflows/summary": () => jsonResponse({ awaiting_my_decision: 0 }),
      [`/api/v1/workflows/${WF_ID}`]: () =>
        jsonResponse(
          workflow({
            actions: [
              action({
                can_decide: false,
                blockers: ["You started this workflow: someone else must decide (maker-checker)."],
              }),
            ],
          }),
        ),
    });
    renderApp(`/workflows/${WF_ID}`);
    const why = await screen.findByLabelText("Why you cannot decide");
    expect(within(why).getByText(/maker-checker/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    const history = screen.getByRole("table", { name: "Action history" });
    expect(within(history).getByText("system for Finance Analyst")).toBeInTheDocument();
  });

  it("shows the server's refusal when a decision fails", async () => {
    mockFetch({
      "/api/v1/auth/me": me(MANAGER_PERMISSIONS, { role: "MANAGER" }),
      "/api/v1/workflows/summary": () => jsonResponse({ awaiting_my_decision: 1 }),
      [`/api/v1/workflows/${WF_ID}`]: () => jsonResponse(workflow()),
      [`/api/v1/workflows/${WF_ID}/reject`]: () =>
        problem(409, "The workflow is not awaiting approval (it is completed)."),
    });
    renderApp(`/workflows/${WF_ID}`);
    const form = await screen.findByRole("form", { name: "Decision" });
    await userEvent.type(within(form).getByLabelText(/Reason/), "Wrong vendor.");
    await userEvent.click(within(form).getByRole("button", { name: "Reject" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("not awaiting approval");
  });
});

describe("Reports", () => {
  it("shows a report as text and verifies it", async () => {
    const report: Report = {
      id: REPORT_ID,
      report_type: "INVOICE_VERIFICATION",
      subject_type: "DOCUMENT",
      subject_id: DOC_ID,
      title: "Invoice verification: invoice-1001.pdf",
      document_ids: [DOC_ID],
      workflow_id: WF_ID,
      template_version: 1,
      content_sha256: "a".repeat(64),
      as_of: "2026-10-10T10:05:00Z",
      generated_by_email: "manager@docintel.local",
      created_at: "2026-10-10T10:05:01Z",
      content: "# Invoice verification: invoice-1001.pdf\n\n<script>alert(1)</script>",
      snapshot: {},
    };
    mockFetch({
      "/api/v1/auth/me": me(VIEWER, { role: "VIEWER" }),
      [`/api/v1/reports/${REPORT_ID}`]: () => jsonResponse(report),
      [`/api/v1/reports/${REPORT_ID}/verify`]: () =>
        jsonResponse({ report_id: REPORT_ID, matches: true, content_sha256: report.content_sha256 }),
    });
    renderApp(`/reports/${REPORT_ID}`);
    const content = await screen.findByLabelText("Report content");
    expect(content.textContent).toContain("<script>alert(1)</script>"); // text, never HTML
    expect(document.querySelector("script")).toBeNull();
    expect(screen.getByRole("link", { name: "workflow" })).toHaveAttribute("href", `/workflows/${WF_ID}`);
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Verified");
  });
});

describe("Administration", () => {
  it("lets an administrator create a user and see the audit log", async () => {
    let created: unknown = null;
    const users: UserPage = {
      items: [
        {
          ...CURRENT_USER, role: "ADMIN", department: null, failed_login_attempts: 0, locked_until: null,
          created_at: "2026-10-01T00:00:00Z",
        },
      ],
      total: 1,
      limit: 200,
      offset: 0,
    };
    const events: AuditEventPage = {
      items: [
        {
          id: 42, occurred_at: "2026-10-10T10:05:00Z", actor: MANAGER, actor_type: "USER", actor_role: "MANAGER",
          action: "workflow.action.approved", entity_type: "workflow_action", entity_id: ACTION_ID,
          outcome: "SUCCESS", request_id: "r", ip_address: "203.0.113.10", user_agent: null,
          details: { action_type: "APPROVE_FOR_PAYMENT" },
        },
      ],
      next_before_id: null,
    };
    mockFetch({
      "/api/v1/auth/me": me(ADMIN, { role: "ADMIN", department: null }),
      "/api/v1/workflows/summary": () => jsonResponse({ awaiting_my_decision: 0 }),
      "/api/v1/departments": () => jsonResponse([CURRENT_USER.department]),
      "/api/v1/users?q=&limit=200": () => jsonResponse(users),
      "/api/v1/users": (_url, init) => {
        created = JSON.parse(init?.body as string);
        return jsonResponse({ ...users.items[0], email: "new@example.test" }, 201);
      },
      "/api/v1/audit-logs?limit=50": () => jsonResponse(events),
    });
    const { router } = renderApp("/admin/users");
    const form = await screen.findByRole("form", { name: "New user" });
    await userEvent.type(within(form).getByLabelText("Email"), "new@example.test");
    await userEvent.type(within(form).getByLabelText("Full name"), "New Reviewer");
    await userEvent.selectOptions(within(form).getByLabelText("Role"), "REVIEWER");
    await userEvent.type(within(form).getByLabelText(/Initial password/), "a long enough passphrase");
    await userEvent.click(within(form).getByRole("button", { name: "Create user" }));
    await waitFor(() => {
      expect(created).toEqual({
        email: "new@example.test",
        full_name: "New Reviewer",
        role: "REVIEWER",
        department_id: CURRENT_USER.department.id,
        password: "a long enough passphrase",
      });
    });
    // Administrators cannot change their own role or deactivate themselves here either.
    const own = screen.getByRole("row", { name: CURRENT_USER.email });
    expect(within(own).getByLabelText(`Role of ${CURRENT_USER.email}`)).toBeDisabled();

    await router.navigate("/admin/audit");
    const table = await screen.findByRole("region", { name: "Audit events" });
    expect(await within(table).findByText("workflow.action.approved")).toBeInTheDocument();
    expect(within(table).getByText("203.0.113.10")).toBeInTheDocument();
  });
});
