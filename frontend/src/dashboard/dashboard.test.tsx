import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { documentsUrl } from "../documents/format";
import type { DashboardSummary } from "../lib/types";
import { browserHasSession, CURRENT_USER, jsonResponse, mockFetch, renderApp } from "../test/utils";

const DOC_ID = "5b0d8a3e-6b43-4c86-9f73-0b5b0f1f0a11";
const WORKFLOW_ID = "7a0c1b2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d";

function summary(days = 30): DashboardSummary {
  const confidence = Array.from({ length: days }, (_, index) => {
    const day = new Date(Date.UTC(2026, 9, 10 - (days - 1) + index)).toISOString().slice(0, 10);
    const today = index === days - 1;
    return {
      day,
      processed: today ? 4 : 0,
      extraction_confidence: today ? 0.91 : null,
      auto_accepted: today ? 3 : 0,
    };
  });
  return {
    days,
    since: "2026-09-10T08:00:00Z",
    until: "2026-10-10T08:00:00Z",
    documents: {
      total: 1284,
      uploaded_in_period: 40,
      by_status: { COMPLETED: 30, REVIEW_REQUIRED: 9, FAILED: 1 },
      by_type: { INVOICE: 20, PURCHASE_ORDER: 15, UNCLASSIFIED: 5 },
    },
    processing: { processed_in_period: 40, average_seconds: 3.4, p95_seconds: 7.9, failed_in_period: 1 },
    review_queue: { open: 9, overdue: 2, by_priority: { HIGH: 4, NORMAL: 5 }, by_type: { DISCREPANCY_REVIEW: 9 } },
    discrepancies: {
      documents_failing: 6,
      by_rule: [
        { rule_code: "INV_PO_UNIT_PRICE", documents: 4 },
        { rule_code: "INV_MISSING_PO", documents: 2 },
      ],
    },
    investigations: {
      scope: "mine",
      in_period: 5,
      by_status: { COMPLETED: 5 },
      by_recommendation: { REQUEST_VENDOR_CLARIFICATION: 3, APPROVE_FOR_PAYMENT: 2 },
    },
    workflows: {
      awaiting_approval: 3,
      finished_in_period: 7,
      by_outcome: { APPROVED_FOR_PAYMENT: 5, AWAITING_VENDOR_CLARIFICATION: 2 },
      by_status: { COMPLETED: 7, AWAITING_APPROVAL: 3 },
    },
    confidence,
    activity: [
      {
        id: 41,
        occurred_at: "2026-10-10T07:59:00Z",
        action: "workflow.action.approved",
        outcome: "SUCCESS",
        actor: "Finance Reviewer",
        document_id: DOC_ID,
        document_name: "B0001-INV.pdf",
        workflow_id: WORKFLOW_ID,
      },
      {
        id: 40,
        occurred_at: "2026-10-10T07:58:00Z",
        action: "document.uploaded",
        outcome: "SUCCESS",
        actor: "Finance Analyst",
        document_id: DOC_ID,
        document_name: "B0001-INV.pdf",
        workflow_id: null,
      },
    ],
  };
}

const ME = { ...CURRENT_USER, permissions: ["dashboard:read", "documents:read"] };

describe("dashboard", () => {
  it("is the home page and summarizes the department's documents", async () => {
    browserHasSession();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(ME),
      "/api/v1/dashboard/summary?days=30": () => jsonResponse(summary()),
    });
    const { router } = renderApp("/");

    const figures = await screen.findByRole("list", { name: "Key figures" });
    expect(router.state.location.pathname).toBe("/dashboard");
    expect(screen.getByText("Finance: documents, checks, reviews and decisions.")).toBeInTheDocument();
    const tiles = within(figures).getAllByRole("listitem");
    expect(tiles.map((tile) => tile.textContent)).toEqual([
      expect.stringContaining("1.3K"),
      expect.stringContaining("2 overdue"),
      expect.stringContaining("6"),
      expect.stringContaining("7 workflows finished"),
      expect.stringContaining("3.4 s"),
      expect.stringContaining("Your AI investigations"),
    ]);
    expect(within(figures).getByRole("link", { name: /Open review tasks/ })).toHaveAttribute("href", "/reviews");

    // Bars carry their values at the tip; status bars open the filtered inbox.
    const failing = screen.getByRole("list", { name: "Failing rules" });
    expect(within(failing).getByText("INV_PO_UNIT_PRICE").closest("li")).toHaveTextContent("4");
    const statuses = screen.getByRole("list", { name: "Documents by status" });
    expect(within(statuses).getByRole("link", { name: "Needs review" })).toHaveAttribute(
      "href",
      "/documents?status=REVIEW_REQUIRED",
    );
    expect(screen.getByRole("list", { name: "Documents by type" })).toHaveTextContent("Unclassified");
    expect(screen.getByRole("list", { name: "Investigation recommendations" })).toHaveTextContent("Ask the vendor");

    // Recent activity links to the document and the workflow.
    const activity = screen.getByRole("list", { name: "Recent activity" });
    expect(within(activity).getAllByRole("link", { name: "B0001-INV.pdf" })[0]).toHaveAttribute(
      "href",
      `/documents/${DOC_ID}`,
    );
    expect(within(activity).getByRole("link", { name: "(workflow)" })).toHaveAttribute(
      "href",
      `/workflows/${WORKFLOW_ID}`,
    );
    expect(activity).toHaveTextContent("approved a proposal on");
  });

  it("every chart has a table twin and a keyboard-reachable readout", async () => {
    browserHasSession();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(ME),
      "/api/v1/dashboard/summary?days=30": () => jsonResponse(summary()),
    });
    const user = userEvent.setup();
    renderApp("/dashboard");

    const trend = await screen.findByRole("figure", { name: "Extraction confidence" });
    await user.click(within(trend).getByText("Show as table"));
    expect(within(trend).getByRole("table")).toHaveTextContent("91.0%");

    const chart = within(trend).getByRole("img", { name: "Extraction confidence per day" });
    fireEvent.focus(chart);
    expect(within(trend).getByRole("status")).toHaveTextContent("91%");
    fireEvent.keyDown(chart, { key: "ArrowLeft" });
    expect(within(trend).getByRole("status")).toHaveTextContent("No data");

    const volume = screen.getByRole("figure", { name: "Documents extracted per day" });
    // Legend (and table header) name both parts.
    expect(within(volume).getAllByText("Sent to review").length).toBeGreaterThan(0);
    fireEvent.focus(within(volume).getByRole("img"));
    expect(within(volume).getByRole("status")).toHaveTextContent("3accepted automatically");
    expect(within(volume).getByRole("status")).toHaveTextContent("1sent to review");
  });

  it("changes the period for every figure at once", async () => {
    browserHasSession();
    const fetchMock = mockFetch({
      "/api/v1/auth/me": () => jsonResponse(ME),
      "/api/v1/dashboard/summary?days=30": () => jsonResponse(summary()),
      "/api/v1/dashboard/summary?days=7": () => jsonResponse(summary(7)),
    });
    const user = userEvent.setup();
    renderApp("/dashboard");

    await screen.findByRole("list", { name: "Key figures" });
    await user.selectOptions(screen.getByLabelText("Period"), "7");
    const trend = screen.getByRole("figure", { name: "Extraction confidence" });
    await user.click(within(trend).getByText("Show as table"));
    expect(await within(trend).findAllByRole("row")).toHaveLength(8); // header + 7 days
    expect(fetchMock.mock.calls.some(([url]) => url === "/api/v1/dashboard/summary?days=7")).toBe(true);
  });

  it("opens the inbox filtered by the status a bar links to", async () => {
    browserHasSession();
    const filtered = documentsUrl({ status: "REVIEW_REQUIRED", q: "", offset: 0 });
    const fetchMock = mockFetch({
      "/api/v1/auth/me": () => jsonResponse(ME),
      [filtered]: () => jsonResponse({ items: [], total: 0, limit: 25, offset: 0 }),
    });
    renderApp("/documents?status=REVIEW_REQUIRED");

    expect(await screen.findByText("No documents match.")).toBeInTheDocument();
    expect(screen.getByLabelText("Filter by status")).toHaveValue("REVIEW_REQUIRED");
    expect(fetchMock.mock.calls.some(([url]) => url === filtered)).toBe(true);
  });
});
