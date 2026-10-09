import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { saveSession } from "../auth/session";
import type { DocumentSearchResponse, DocumentSummary } from "../lib/types";
import { CURRENT_USER, jsonResponse, mockFetch, problem, renderApp } from "../test/utils";

const DOC_ID = "5b0d8a3e-6b43-4c86-9f73-0b5b0f1f0a11";

function me() {
  saveSession({ token: "header.payload.signature", expiresAt: Date.now() + 60_000 });
  const user = { ...CURRENT_USER, permissions: ["documents:read", "knowledge:read"] };
  return () => jsonResponse(user);
}

function document(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: DOC_ID,
    display_filename: "B0002-INV.pdf",
    document_type: "INVOICE",
    type_confidence: "0.9700",
    status: "COMPLETED",
    sensitivity: "INTERNAL",
    source: "UPLOAD",
    owner: { id: CURRENT_USER.id, full_name: CURRENT_USER.full_name },
    department: CURRENT_USER.department,
    duplicate_of_id: null,
    duplicate_reason: null,
    processing_error: null,
    review_reasons: [],
    last_processed_at: "2026-10-08T10:00:00Z",
    created_at: "2026-10-08T09:59:00Z",
    updated_at: "2026-10-08T10:00:00Z",
    current_version: null,
    vendor: null,
    review: null,
    ...overrides,
  };
}

function response(overrides: Partial<DocumentSearchResponse> = {}): DocumentSearchResponse {
  return {
    query: "documents with payment terms longer than 45 days",
    mode: "structured",
    total: 1,
    interpretation: {
      document_types: [],
      vendor: null,
      vendors_matched: [],
      payment_terms_days: { op: "gt", value: "45" },
      total: null,
      date_from: null,
      date_to: null,
      text: "",
      recognized: ["payment terms more than 45 days"],
    },
    results: [
      {
        document: document(),
        vendor_name: "Harbor & Pine Packaging Ltd.",
        document_date: "2026-04-10",
        total: "2909.86",
        payment_terms_days: 60,
        score: null,
        reasons: ["payment terms 60 days (extracted)"],
        snippet: null,
      },
    ],
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("document search", () => {
  it("runs an example and explains how it was understood", async () => {
    let body: unknown = null;
    mockFetch({
      "/api/v1/auth/me": me(),
      "/api/v1/search": (_url, init) => {
        body = JSON.parse(init?.body as string);
        return jsonResponse(response());
      },
    });
    const user = userEvent.setup();
    renderApp("/search");
    await user.click(await screen.findByRole("button", { name: "documents with payment terms longer than 45 days" }));
    const results = await screen.findByRole("region", { name: "Results" });
    expect(body).toEqual({ query: "documents with payment terms longer than 45 days" });
    expect(screen.getByLabelText("Search")).toHaveValue("documents with payment terms longer than 45 days");
    expect(within(results).getByText("payment terms more than 45 days")).toBeInTheDocument();
    expect(within(results).getByRole("link", { name: "B0002-INV.pdf" })).toHaveAttribute("href", `/documents/${DOC_ID}`);
    expect(within(results).getByText(/Harbor & Pine Packaging Ltd\..*payment terms 60 days/)).toBeInTheDocument();
    expect(within(results).getByText("Matched: payment terms 60 days (extracted)")).toBeInTheDocument();
  });

  it("shows text matches with their snippet and free-text interpretation", async () => {
    mockFetch({
      "/api/v1/auth/me": me(),
      "/api/v1/search": () =>
        jsonResponse(
          response({
            mode: "structured+text",
            total: 0,
            interpretation: {
              ...response().interpretation,
              document_types: ["CONTRACT"],
              payment_terms_days: null,
              text: "termination clauses",
              recognized: ["type CONTRACT"],
            },
            results: [],
          }),
        ),
    });
    const user = userEvent.setup();
    renderApp("/search");
    await user.type(await screen.findByLabelText("Search"), "contracts containing termination clauses");
    await user.click(screen.getByRole("button", { name: "Search" }));
    const results = await screen.findByRole("region", { name: "Results" });
    expect(within(results).getByText("type CONTRACT")).toBeInTheDocument();
    expect(within(results).getByText("text “termination clauses”")).toBeInTheDocument();
    expect(within(results).getByText("No documents found.")).toBeInTheDocument();
  });

  it("shows API errors", async () => {
    mockFetch({
      "/api/v1/auth/me": me(),
      "/api/v1/search": () => problem(422, "query: must not contain control characters"),
    });
    const user = userEvent.setup();
    renderApp("/search");
    await user.type(await screen.findByLabelText("Search"), "invoices");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("must not contain control characters");
  });
});
