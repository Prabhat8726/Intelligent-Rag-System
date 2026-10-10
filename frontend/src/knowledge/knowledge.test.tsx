import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { documentsUrl } from "../documents/format";
import type { KnowledgeAnswer, KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentDetail } from "../lib/types";
import { browserHasSession, CURRENT_USER, jsonResponse, mockFetch, problem, renderApp } from "../test/utils";
import { knowledgeUrl } from "./format";

const DOC_ID = "4a1b2c3d-0000-4000-8000-000000000001";
const LIST_URL = knowledgeUrl({ status: "", category: "", q: "", offset: 0 });
const DETAIL_URL = `/api/v1/knowledge/documents/${DOC_ID}`;
// Role permissions as in backend/src/docintel/auth/permissions.py.
const VIEWER = ["documents:read", "rules:read", "knowledge:read"];
const MANAGER = [...VIEWER, "knowledge:manage", "documents:upload"];

/** Signs in now (the app checks the session on mount) and answers GET /auth/me. */
function me(permissions: string[]) {
  browserHasSession();
  const user = { ...CURRENT_USER, permissions };
  return () => jsonResponse(user);
}

function knowledgeDocument(overrides: Partial<KnowledgeDocument> = {}): KnowledgeDocument {
  return {
    id: DOC_ID,
    document_key: "procurement-policy",
    title: "Procurement Policy",
    category: "POLICY",
    version_label: "2026.1",
    department: null,
    sensitivity: "INTERNAL",
    effective_sensitivity: "INTERNAL",
    effective_from: "2026-01-01",
    effective_to: null,
    status: "ACTIVE",
    supersedes_id: null,
    source_format: "MARKDOWN",
    original_filename: "procurement-policy-2026.md",
    size_bytes: 4096,
    page_count: null,
    chunk_count: 14,
    embedding_model: null,
    embedding_note: "no embedding provider is configured (full-text search only)",
    processing_error: null,
    processed_at: "2026-10-09T10:00:00Z",
    uploaded_by: { id: CURRENT_USER.id, full_name: "Platform Administrator" },
    created_at: "2026-10-09T09:59:00Z",
    updated_at: "2026-10-09T10:00:00Z",
    ...overrides,
  };
}

function page(items: KnowledgeDocument[]) {
  return { items, total: items.length, limit: 50, offset: 0 };
}

function answer(overrides: Partial<KnowledgeAnswer> = {}): KnowledgeAnswer {
  return {
    question: "Who approves payment terms longer than 60 days?",
    status: "ANSWERED",
    answer: "Payment terms longer than 60 days need written approval of the CFO. [S1]",
    claims: [
      {
        text: "Payment terms longer than 60 days need written approval of the CFO.",
        citations: ["S1"],
        grounded: true,
        grounding: 0.9,
      },
    ],
    sources: [
      {
        label: "S1",
        cited: true,
        sent_to_model: true,
        knowledge_document_id: DOC_ID,
        document_key: "procurement-policy",
        title: "Procurement Policy",
        version_label: "2026.1",
        status: "ACTIVE",
        section_path: "7. Payment terms",
        page_start: null,
        page_end: null,
        effective_from: "2026-01-01",
        effective_to: null,
        chunk_ids: ["c1"],
        content: "Payment terms longer than 60 days are not allowed without written approval of the CFO.",
      },
      {
        label: "S2",
        cited: false,
        sent_to_model: false,
        knowledge_document_id: "5b2c3d4e-0000-4000-8000-000000000002",
        document_key: "legal-negotiation-playbook",
        title: "Legal Negotiation Playbook",
        version_label: "2026.1",
        status: "ACTIVE",
        section_path: "4. Payment terms fallback",
        page_start: null,
        page_end: null,
        effective_from: "2026-01-01",
        effective_to: null,
        chunk_ids: ["c2"],
        content: "Legal supports Procurement in negotiating 60-day payment terms.",
      },
    ],
    evidence: { sufficient: true, term_coverage: 0.8, dense_similarity: null, reason: "ok" },
    retrieval: { mode: "full_text", embedding_model: null, as_of: "2026-10-09", query_terms: [], timings_ms: {} },
    notices: ["1 source(s) above the external AI sensitivity limit were not sent to the model."],
    model: "scripted-default",
    provider: "scripted",
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("knowledge base", () => {
  it("is in the navigation for roles that may read it", async () => {
    mockFetch({
      "/api/v1/auth/me": me(VIEWER),
      [LIST_URL]: () => jsonResponse(page([knowledgeDocument()])),
    });
    renderApp("/knowledge");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Knowledge" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Search" })).toBeInTheDocument();
    const library = await screen.findByRole("region", { name: "Library" });
    expect(within(library).getByRole("link", { name: "Procurement Policy" })).toHaveAttribute(
      "href",
      `/knowledge/${DOC_ID}`,
    );
    expect(within(library).getByText("Everyone")).toBeInTheDocument();
    expect(within(library).getByText("from 2026-01-01")).toBeInTheDocument();
    expect(within(library).getByText("full-text only")).toBeInTheDocument();
    // Viewers ask questions but do not manage the library.
    expect(screen.getByRole("form", { name: "Ask the knowledge base" })).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Add knowledge document" })).not.toBeInTheDocument();
  });

  it("is hidden from roles without knowledge access", async () => {
    mockFetch({
      "/api/v1/auth/me": me(["documents:read"]),
      [documentsUrl({ status: "", q: "", offset: 0 })]: () => jsonResponse({ items: [], total: 0, limit: 25, offset: 0 }),
    });
    renderApp("/documents");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).queryByRole("link", { name: "Knowledge" })).not.toBeInTheDocument();
  });

  it("shows a cited answer with its sources", async () => {
    let body: unknown = null;
    mockFetch({
      "/api/v1/auth/me": me(VIEWER),
      [LIST_URL]: () => jsonResponse(page([])),
      "/api/v1/knowledge/query": (_url, init) => {
        body = JSON.parse(init?.body as string);
        return jsonResponse(answer());
      },
    });
    const user = userEvent.setup();
    renderApp("/knowledge");
    const ask = await screen.findByRole("form", { name: "Ask the knowledge base" });
    await user.type(within(ask).getByLabelText(/Ask about policies/), "Who approves payment terms longer than 60 days?");
    await user.selectOptions(within(ask).getByLabelText("Category"), "POLICY");
    await user.click(within(ask).getByRole("button", { name: "Ask" }));

    const result = await screen.findByRole("region", { name: "Answer" });
    expect(body).toEqual({
      question: "Who approves payment terms longer than 60 days?",
      categories: ["POLICY"],
    });
    expect(within(result).getByText("Answered")).toBeInTheDocument();
    const statements = within(result).getByRole("list", { name: "Statements" });
    expect(within(statements).getByRole("link", { name: "Source S1" })).toHaveAttribute("href", "#source-S1");
    expect(within(result).getByText("cited")).toBeInTheDocument();
    expect(within(result).getByText("not sent to model")).toBeInTheDocument();
    expect(within(result).getByText(/above the external AI sensitivity limit/)).toBeInTheDocument();
    // Cited sources are open; the others can be expanded.
    expect(within(result).getByText(/not allowed without written approval/)).toBeInTheDocument();
    expect(within(result).queryByText(/negotiating 60-day payment terms/)).not.toBeInTheDocument();
    await user.click(within(result).getByRole("button", { name: "Show passage" })); // S2 only
    expect(within(result).getByText(/negotiating 60-day payment terms/)).toBeInTheDocument();
  });

  it("says when the knowledge base has no answer", async () => {
    mockFetch({
      "/api/v1/auth/me": me(VIEWER),
      [LIST_URL]: () => jsonResponse(page([])),
      "/api/v1/knowledge/query": () =>
        jsonResponse(
          answer({ status: "INSUFFICIENT_EVIDENCE", answer: null, claims: [], notices: [], model: null, provider: null }),
        ),
    });
    const user = userEvent.setup();
    renderApp("/knowledge");
    await user.type(await screen.findByLabelText(/Ask about policies/), "What is the dress code?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    const result = await screen.findByRole("region", { name: "Answer" });
    expect(within(result).getByText("Insufficient evidence")).toBeInTheDocument();
    // No model saw anything, so no source is marked as withheld.
    expect(within(result).queryByText("not sent to model")).not.toBeInTheDocument();
    expect(within(result).getByText("Closest passages")).toBeInTheDocument();
    expect(within(result).queryByRole("list", { name: "Statements" })).not.toBeInTheDocument();
  });

  it("lets managers add a document for their department", async () => {
    let form: FormData | null = null;
    mockFetch({
      "/api/v1/auth/me": me(MANAGER),
      [LIST_URL]: () => jsonResponse(page([])),
      "/api/v1/knowledge/documents": (_url, init) => {
        form = init?.body as FormData;
        return jsonResponse(knowledgeDocument({ status: "PROCESSING", title: "Finance FAQ" }), 201);
      },
    });
    const user = userEvent.setup();
    renderApp("/knowledge");
    const upload = await screen.findByRole("form", { name: "Add knowledge document" });
    await user.upload(
      within(upload).getByLabelText(/Document \(Markdown/),
      new File(["# Finance FAQ\n\nQ1. Text."], "faq.md", { type: "text/markdown" }),
    );
    await user.selectOptions(within(upload).getByLabelText("Category"), "FAQ");
    await user.click(within(upload).getByLabelText("Only Finance"));
    await user.click(within(upload).getByRole("button", { name: "Add" }));
    expect(await within(upload).findByRole("status")).toHaveTextContent("Added “Finance FAQ”");
    expect(form).not.toBeNull();
    const sent = form as unknown as FormData;
    expect(sent.get("category")).toBe("FAQ");
    expect(sent.get("department_id")).toBe(CURRENT_USER.department.id);
    expect(sent.get("title")).toBeNull(); // empty fields come from the front matter
  });

  it("shows upload problems from the API", async () => {
    mockFetch({
      "/api/v1/auth/me": me(MANAGER),
      [LIST_URL]: () => jsonResponse(page([])),
      "/api/v1/knowledge/documents": () => problem(422, "category is required (POLICY, PROCEDURE, ...)."),
    });
    const user = userEvent.setup();
    renderApp("/knowledge");
    const upload = await screen.findByRole("form", { name: "Add knowledge document" });
    await user.upload(
      within(upload).getByLabelText(/Document \(Markdown/),
      new File(["# Notes"], "notes.md", { type: "text/markdown" }),
    );
    await user.click(within(upload).getByRole("button", { name: "Add" }));
    expect(await within(upload).findByRole("alert")).toHaveTextContent("category is required");
  });

  it("shows a document's passages and archives it", async () => {
    const chunks: KnowledgeChunk[] = [
      {
        id: "c1",
        chunk_index: 0,
        section_path: "4. Price variance › 4.1 Tolerance",
        heading: "4.1 Tolerance",
        kind: "text",
        content: "A difference of up to 0.01 per unit is accepted.",
        page_start: null,
        page_end: null,
        token_count: 32,
        embedding_model: null,
        has_embedding: false,
        effective_from: "2026-01-01",
        effective_to: null,
      },
    ];
    const detail: KnowledgeDocumentDetail = { ...knowledgeDocument(), sha256: "a".repeat(64), mime_type: "text/markdown", latest_job: null };
    let deleted = false;
    mockFetch({
      "/api/v1/auth/me": me(MANAGER),
      [DETAIL_URL]: (_url, init) => {
        if (init?.method === "DELETE") {
          deleted = true;
          return new Response(null, { status: 204 });
        }
        return jsonResponse(detail);
      },
      [`${DETAIL_URL}/chunks`]: () => jsonResponse(chunks),
      [LIST_URL]: () => jsonResponse(page([])),
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    const { router } = renderApp(`/knowledge/${DOC_ID}`);
    expect(await screen.findByRole("heading", { name: "Procurement Policy" })).toBeInTheDocument();
    const passages = await screen.findByRole("region", { name: "Passages" });
    expect(within(passages).getByText("4. Price variance › 4.1 Tolerance")).toBeInTheDocument();
    expect(screen.getByText("none (full-text only)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Archive" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/knowledge");
    });
    expect(deleted).toBe(true);
  });

  it("reports documents that are not available", async () => {
    mockFetch({
      "/api/v1/auth/me": me(VIEWER),
      [DETAIL_URL]: () => problem(404, "Knowledge document not found."),
    });
    renderApp(`/knowledge/${DOC_ID}`);
    expect(await screen.findByRole("alert")).toHaveTextContent("does not exist or is not available to you");
  });
});
