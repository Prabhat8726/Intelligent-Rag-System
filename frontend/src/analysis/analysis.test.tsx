import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AnalysisRun, ApiToken } from "../lib/types";
import { browserHasSession, CURRENT_USER, jsonResponse, mockFetch, problem, renderApp } from "../test/utils";

const DOC_ID = "5b0d8a3e-6b43-4c86-9f73-0b5b0f1f0a11";
const PO_ID = "6c1e9b4f-7c54-4d97-8a84-1c6c1f2f1b22";
const RUN_ID = "9a8b7c6d-0000-4000-8000-0000000000aa";
const TASK_ID = "1f2e3d4c-0000-4000-8000-0000000000bb";
// Role permissions as in backend/src/docintel/auth/permissions.py.
const VIEWER = ["documents:read", "knowledge:read", "analysis:read"];
const REVIEWER = [...VIEWER, "analysis:run", "reviews:work", "comparisons:create"];

function me(permissions: string[]) {
  browserHasSession();
  return () => jsonResponse({ ...CURRENT_USER, permissions });
}

function run(overrides: Partial<AnalysisRun> = {}): AnalysisRun {
  return {
    id: RUN_ID,
    status: "COMPLETED",
    query: "Can we pay this invoice?",
    document_ids: [DOC_ID],
    intent: "VERIFY_DOCUMENT",
    recommendation: "HOLD_FOR_REVIEW",
    confidence: "MEDIUM",
    error: null,
    created_at: "2026-10-09T10:00:00Z",
    started_at: "2026-10-09T10:00:01Z",
    finished_at: "2026-10-09T10:00:02Z",
    graph_version: "investigation-v1",
    allow_safe_actions: true,
    plan: {
      intent: "VERIFY_DOCUMENT",
      document_query: null,
      identifiers: [],
      knowledge_questions: [],
      focus_fields: [],
      source: "rules",
    },
    result: {
      summary: "INV.pdf (invoice): 1 rule(s) failed (Unit price differs from the purchase order).",
      summary_source: "rules",
      intent: "VERIFY_DOCUMENT",
      documents: [
        {
          label: "D1", role: "subject", document_id: DOC_ID, filename: "INV.pdf", document_type: "INVOICE",
          status: "REVIEW_REQUIRED", vendor_name: "Kestrel Industrial Supply Inc.", document_date: "2026-05-26",
          total: "4367.78", currency: "USD", effective_sensitivity: "INTERNAL",
        },
        {
          label: "D2", role: "related", document_id: PO_ID, filename: "PO.pdf", document_type: "PURCHASE_ORDER",
          status: "COMPLETED", vendor_name: null, document_date: null, total: null, currency: null,
          effective_sensitivity: "INTERNAL",
        },
      ],
      findings: [
        {
          category: "OBSERVED_FACT", statement: "INV.pdf: invoice, total 4367.78 USD.", evidence: ["D1"],
          source: "rules", grounded: true,
        },
        {
          category: "RULE_RESULT",
          statement: "Unit price differs from the purchase order (INV_PO_UNIT_PRICE, HIGH): FAIL",
          evidence: ["D1.R12", "D1.C1"], source: "rules", grounded: true,
        },
        {
          category: "AI_INFERENCE", statement: "The price difference exceeds what the policy tolerates.",
          evidence: ["D1.R12", "K1"], source: "model", grounded: true,
        },
      ],
      evidence: [
        { label: "D1", kind: "DOCUMENT", document_id: DOC_ID, ref: DOC_ID, text: "INV.pdf: invoice" },
        { label: "D1.R12", kind: "RULE", document_id: DOC_ID, ref: "INV_PO_UNIT_PRICE", text: "INV_PO_UNIT_PRICE: FAIL" },
        { label: "D1.C1", kind: "COMPARISON", document_id: DOC_ID, ref: "line:CHR-ERG2", text: "unit_price: MISMATCH" },
        { label: "K1", kind: "KNOWLEDGE", document_id: null, ref: "c1", text: "Procurement Policy - 4.1 Tolerance" },
      ],
      sources: [
        {
          label: "K1", sent_to_model: true, chunk_id: "c1", knowledge_document_id: "k1", title: "Procurement Policy",
          version_label: "2026.1", section_path: "4. Price variance › 4.1 Tolerance", page_start: null,
          page_end: null, effective_from: "2026-01-01", effective_to: null,
          content: "Invoice prices may exceed the order price by at most 2 percent.", query: "Unit price differs",
        },
      ],
      comparisons: [],
      confidence: {
        level: "MEDIUM",
        score: 0.75,
        factors: [{ factor: "extraction", effect: -0.15, detail: "Some extracted values of INV.pdf are uncertain." }],
      },
      recommendation: {
        action: "HOLD_FOR_REVIEW", target_document_id: DOC_ID,
        rationale: "Failed rule(s) need a reviewer's decision: Unit price differs from the purchase order.",
        evidence: [], risk: "LOW", requires_approval: false, required_role: null, source: "rules",
        guardrail_notes: ["The model proposed APPROVE_FOR_PAYMENT, not allowed: a rule failed."],
      },
      action: {
        action: "HOLD_FOR_REVIEW", status: "EXECUTED", detail: "Review requested on the document's review task.",
        tool_call_id: "t1", review_task_id: TASK_ID, required_role: null,
      },
      notices: [],
      model: { provider: "gemini", model: "gemini-flash" },
    },
    trace: [
      { node: "understand_request", duration_ms: 1.2, tool_calls: 0 },
      { node: "run_rules", duration_ms: 40.5, tool_calls: 3 },
    ],
    tool_call_log: [
      {
        id: "t0", node_name: "run_rules", tool_name: "run_business_rules", status: "SUCCEEDED", error: null,
        latency_ms: "35.20", arguments: {}, result_summary: null, created_at: "2026-10-09T10:00:01Z",
      },
    ],
    usage: { tool_calls: 9, llm_calls: 2, input_tokens: 900, output_tokens: 120, estimated_cost_usd: null },
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AI analysis", () => {
  it("starts an investigation of a document and shows its result", async () => {
    let body: unknown = null;
    mockFetch({
      "/api/v1/auth/me": me(REVIEWER),
      "/api/v1/analysis?limit=25": () => jsonResponse({ items: [], total: 0, limit: 25, offset: 0 }),
      [`/api/v1/documents/${DOC_ID}`]: () => jsonResponse({ id: DOC_ID, display_filename: "INV.pdf" }),
      "/api/v1/analysis": (_url, init) => {
        body = JSON.parse(init?.body as string);
        return jsonResponse(run({ status: "QUEUED", result: null, trace: [], tool_call_log: [] }), 202);
      },
      [`/api/v1/analysis/${RUN_ID}`]: () => jsonResponse(run()),
    });
    const user = userEvent.setup();
    renderApp(`/analysis?document=${DOC_ID}`);
    const form = await screen.findByRole("form", { name: "New investigation" });
    expect(await within(form).findByText("INV.pdf")).toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: "Can we pay this invoice?" }));
    await user.click(within(form).getByRole("button", { name: "Investigate" }));

    const recommendation = await screen.findByRole("region", { name: "Recommendation" });
    expect(body).toEqual({ query: "Can we pay this invoice?", document_ids: [DOC_ID], allow_safe_actions: true });
    expect(within(recommendation).getByRole("heading", { name: "Hold for review" })).toBeInTheDocument();
    expect(within(recommendation).getByText(/Guardrail: The model proposed APPROVE_FOR_PAYMENT/)).toBeInTheDocument();
    expect(within(recommendation).getByLabelText("Action")).toHaveTextContent(
      "Done: Review requested on the document's review task.",
    );

    const findings = screen.getByRole("region", { name: "Findings" });
    const [rule, fact, inference] = within(findings).getAllByRole("listitem");
    if (!rule || !fact || !inference) throw new Error("expected three findings");
    // Rule outcomes first, then facts, then interpretation.
    expect(rule).toHaveTextContent("Rule result");
    expect(fact).toHaveTextContent("Observed fact");
    expect(inference).toHaveTextContent("AI inference");
    expect(inference).toHaveTextContent("AI-written, checked");
    expect(within(rule).getByText("D1.C1")).toHaveAttribute("title", "unit_price: MISMATCH");
    expect(within(inference).getByRole("link", { name: "Source K1" })).toHaveAttribute("href", "#source-K1");

    const sources = screen.getByRole("region", { name: "Sources" });
    expect(within(sources).getByRole("link", { name: "Procurement Policy" })).toHaveAttribute("href", "/knowledge/k1");
    const documents = screen.getByRole("region", { name: "Documents" });
    expect(within(documents).getByRole("link", { name: "INV.pdf" })).toHaveAttribute("href", `/documents/${DOC_ID}`);
    expect(screen.getByRole("region", { name: "Confidence" })).toHaveTextContent(
      "Some extracted values of INV.pdf are uncertain.",
    );
    expect(screen.getByText("Steps and tool calls (9 tool calls, 2 model calls)")).toBeInTheDocument();
  });

  it("lists investigations for readers who cannot start one", async () => {
    mockFetch({
      "/api/v1/auth/me": me(VIEWER),
      "/api/v1/analysis?limit=25": () =>
        jsonResponse({ items: [run()], total: 1, limit: 25, offset: 0 }),
    });
    renderApp("/analysis");
    const list = await screen.findByRole("region", { name: "Investigations" });
    expect(await within(list).findByRole("link", { name: "Can we pay this invoice?" })).toHaveAttribute(
      "href",
      `/analysis/${RUN_ID}`,
    );
    expect(within(list).getByText("Hold for review")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "New investigation" })).not.toBeInTheDocument();
  });

  it("shows why an investigation failed and errors from the API", async () => {
    mockFetch({
      "/api/v1/auth/me": me(REVIEWER),
      [`/api/v1/analysis/${RUN_ID}`]: () =>
        jsonResponse(
          run({
            status: "FAILED",
            result: null,
            error: "The investigation exceeded its time limit (AGENT_TIMEOUT_SECONDS).",
          }),
        ),
      "/api/v1/analysis?limit=25": () => jsonResponse({ items: [], total: 0, limit: 25, offset: 0 }),
      "/api/v1/analysis": () => problem(429, "You already have 3 investigations queued or running."),
    });
    const user = userEvent.setup();
    const { router } = renderApp(`/analysis/${RUN_ID}`);
    expect(await screen.findByRole("alert")).toHaveTextContent("exceeded its time limit");
    await router.navigate("/analysis");
    await user.type(await screen.findByLabelText("What should be investigated?"), "Who approves this?");
    await user.click(screen.getByRole("button", { name: "Investigate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("3 investigations queued or running");
  });
});

describe("API tokens", () => {
  const token: ApiToken = {
    id: "7d6c5b4a-0000-4000-8000-0000000000cc",
    name: "Laptop client",
    prefix: "dit_AbCdEfGh",
    scopes: ["documents:read"],
    created_at: "2026-10-09T10:00:00Z",
    expires_at: "2099-11-08T10:00:00Z",
    last_used_at: null,
    revoked_at: null,
  };

  it("creates a token, shows it once and revokes tokens", async () => {
    let created: unknown = null;
    let deleted = false;
    let tokens: ApiToken[] = [];
    mockFetch({
      "/api/v1/auth/me": me(VIEWER),
      "/api/v1/auth/tokens": (_url, init) => {
        if (init?.method === "POST") {
          created = JSON.parse(init.body as string);
          tokens = [token];
          return jsonResponse({ ...token, token: "dit_AbCdEfGh-secret-value" }, 201);
        }
        return jsonResponse(tokens);
      },
      [`/api/v1/auth/tokens/${token.id}`]: () => {
        deleted = true;
        tokens = [{ ...token, revoked_at: "2026-10-09T11:00:00Z" }];
        return new Response(null, { status: 204 });
      },
    });
    const user = userEvent.setup();
    renderApp("/settings/tokens");
    const form = await screen.findByRole("form", { name: "New token" });
    // A viewer can only scope tokens to what they may do themselves.
    expect(within(form).queryByLabelText("Compare documents")).not.toBeInTheDocument();
    await user.type(within(form).getByLabelText("Name"), "Laptop client");
    await user.click(within(form).getByLabelText("Search the knowledge base"));
    await user.click(within(form).getByRole("button", { name: "Create token" }));

    const secret = await screen.findByRole("region", { name: "New token value" });
    expect(secret).toHaveTextContent("dit_AbCdEfGh-secret-value");
    expect(created).toEqual({
      name: "Laptop client",
      scopes: ["documents:read", "knowledge:read"],
      expires_in_days: 30,
    });
    const list = screen.getByRole("region", { name: "Your tokens" });
    await user.click(await within(list).findByRole("button", { name: "Revoke" }));
    await waitFor(() => {
      expect(within(list).getByText(/revoked/)).toBeInTheDocument();
    });
    expect(deleted).toBe(true);
    expect(within(list).queryByText("dit_AbCdEfGh-secret-value")).not.toBeInTheDocument();
  });
});
