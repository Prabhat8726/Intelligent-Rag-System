import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { documentsUrl } from "../documents/format";
import type {
  Comparison,
  ComparisonSide,
  DocumentDetail,
  DocumentSummary,
  Findings,
  Page,
  ReviewTask,
  ReviewTaskListItem,
  Rule,
  VersionComparison,
  VersionInfo,
} from "../lib/types";
import { browserHasSession, CURRENT_USER, jsonResponse, mockFetch, problem, renderApp } from "../test/utils";
import { itemValue, reviewTasksUrl } from "./format";

const DOC_ID = "5b0d8a3e-6b43-4c86-9f73-0b5b0f1f0a11";
const PO_ID = "7d1e9b4f-0000-4c86-9f73-0b5b0f1f0a22";
const COPY_ID = "9a2f0c5d-0000-4c86-9f73-0b5b0f1f0a33";
const TASK_ID = "1c2d3e4f-0000-4000-8000-0000000000aa";
const COMPARISON_ID = "2d3e4f5a-0000-4000-8000-0000000000bb";
const DETAIL_URL = `/api/v1/documents/${DOC_ID}`;
const FINDINGS_URL = `${DETAIL_URL}/findings`;
const VERSIONS_URL = `${DETAIL_URL}/versions`;
const QUEUE_URL = reviewTasksUrl({ state: "open", taskType: "", assigned: "any", offset: 0 });

// Role permissions as in backend/src/docintel/auth/permissions.py.
const ANALYST = [
  "documents:read",
  "documents:upload",
  "documents:process",
  "documents:review",
  "comparisons:create",
  "rules:read",
  "reviews:work",
];
const VIEWER = ["documents:read", "rules:read"];

function signedIn(permissions: string[] = ANALYST) {
  browserHasSession();
  return { ...CURRENT_USER, permissions };
}

const ME = { id: CURRENT_USER.id, full_name: CURRENT_USER.full_name };

function summary(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: DOC_ID,
    display_filename: "invoice-1001.pdf",
    document_type: "INVOICE",
    type_confidence: "0.9700",
    status: "COMPLETED",
    sensitivity: "INTERNAL",
    source: "UPLOAD",
    owner: ME,
    department: CURRENT_USER.department,
    duplicate_of_id: null,
    duplicate_reason: null,
    processing_error: null,
    review_reasons: [],
    last_processed_at: "2026-10-08T10:00:00Z",
    created_at: "2026-10-08T09:59:00Z",
    updated_at: "2026-10-08T10:00:00Z",
    current_version: {
      id: "c3b6c0a2-0000-4000-8000-000000000002",
      version_number: 1,
      original_filename: "invoice-1001.pdf",
      mime_type: "application/pdf",
      size_bytes: 3456,
      sha256: "a".repeat(64),
      page_count: 1,
      created_at: "2026-10-08T09:59:00Z",
    },
    vendor: null,
    review: null,
    ...overrides,
  };
}

function detail(overrides: Partial<DocumentSummary> = {}): DocumentDetail {
  return {
    ...summary(overrides),
    inspection: null,
    sensitivity_assessment: null,
    latest_job: null,
    classification: null,
    classification_history: [],
    pages: [],
  };
}

function task(overrides: Partial<ReviewTask> = {}): ReviewTask {
  return {
    id: TASK_ID,
    document_id: DOC_ID,
    document_version_id: "c3b6c0a2-0000-4000-8000-000000000002",
    task_type: "DISCREPANCY_REVIEW",
    status: "OPEN",
    priority: "HIGH",
    reasons: [
      {
        key: "rule:INV_PO_UNIT_PRICE:FAIL:line_item:HB-10:unit_price",
        category: "RULE",
        code: "INV_PO_UNIT_PRICE",
        severity: "HIGH",
        message: "Unit price of HB-10 is 2.65 USD on the invoice but 2.50 USD on the order.",
      },
    ],
    due_at: "2026-10-09T10:00:00Z",
    assigned_to: null,
    claimed_at: null,
    resolution: null,
    resolution_note: null,
    resolved_by: null,
    resolved_at: null,
    created_at: "2026-10-08T10:00:00Z",
    updated_at: "2026-10-08T10:00:00Z",
    overdue: false,
    ...overrides,
  };
}

function listItem(overrides: Partial<ReviewTask> = {}): ReviewTaskListItem {
  return {
    ...task(overrides),
    document: { id: DOC_ID, display_filename: "invoice-1001.pdf", document_type: "INVOICE", status: "REVIEW_REQUIRED" },
  };
}

function side(overrides: Partial<ComparisonSide>): ComparisonSide {
  return {
    role: "INVOICE",
    document_id: DOC_ID,
    document_label: "invoice-1001.pdf",
    value: null,
    printed: null,
    field_id: null,
    page: 1,
    source_text: null,
    bbox: [100, 300, 200, 312],
    confidence: 0.98,
    corrected: false,
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("review queue", () => {
  it("is in the navigation only for roles that work reviews", async () => {
    const viewer = signedIn(VIEWER);
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(viewer),
      [documentsUrl({ status: "", q: "", offset: 0 })]: () => jsonResponse({ items: [], total: 0, limit: 25, offset: 0 }),
    });
    renderApp("/documents");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Rules" })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Review queue" })).not.toBeInTheDocument();
  });

  it("lists tasks with priority, reasons and due date, and claims one", async () => {
    const user = signedIn();
    let claimed = false;
    const tasks = (): Page<ReviewTaskListItem> => ({
      items: [
        listItem({
          priority: "URGENT",
          overdue: true,
          due_at: "2026-10-08T14:00:00Z",
          assigned_to: claimed ? ME : null,
          status: claimed ? "IN_PROGRESS" : "OPEN",
        }),
      ],
      total: 1,
      limit: 25,
      offset: 0,
    });
    const fetchMock = mockFetch({
      "/api/v1/auth/me": () => jsonResponse(user),
      [QUEUE_URL]: () => jsonResponse(tasks()),
      [reviewTasksUrl({ state: "open", taskType: "", assigned: "me", offset: 0 })]: () => jsonResponse(tasks()),
      [`/api/v1/review-tasks/${TASK_ID}/claim`]: () => {
        claimed = true;
        return jsonResponse(tasks().items[0]);
      },
    });
    const actor = userEvent.setup();
    renderApp("/reviews");

    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Review queue" })).toBeInTheDocument();
    const table = await screen.findByRole("table");
    const row = within(table).getAllByRole("row")[1] as HTMLElement;
    expect(within(row).getByRole("link", { name: "invoice-1001.pdf" })).toHaveAttribute("href", `/documents/${DOC_ID}`);
    expect(within(row).getByText("Urgent")).toBeInTheDocument();
    expect(within(row).getByText("Invoice · Discrepancy")).toBeInTheDocument();
    expect(within(row).getByText(/Unit price of HB-10 is 2.65 USD/)).toBeInTheDocument();
    expect(within(row).getByText("overdue")).toBeInTheDocument();

    await actor.click(within(row).getByRole("button", { name: "Claim" }));
    expect(await within(table).findByRole("button", { name: "Release" })).toBeInTheDocument();
    expect(within(table).getByText(CURRENT_USER.full_name)).toBeInTheDocument();
    const claim = fetchMock.mock.calls.find(([url]) => url === `/api/v1/review-tasks/${TASK_ID}/claim`);
    expect(claim?.[1]?.method).toBe("POST");

    await actor.selectOptions(screen.getByLabelText("Assigned"), "me");
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([url]) => typeof url === "string" && url.includes("assigned=me"))).toBe(true);
    });
  });

  it("shows the server's reason when a claim is refused", async () => {
    const user = signedIn();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(user),
      [QUEUE_URL]: () => jsonResponse({ items: [listItem()], total: 1, limit: 25, offset: 0 }),
      [`/api/v1/review-tasks/${TASK_ID}/claim`]: () => problem(409, "The task is claimed by someone else."),
    });
    const actor = userEvent.setup();
    renderApp("/reviews");
    await actor.click(await screen.findByRole("button", { name: "Claim" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("claimed by someone else");
  });
});

describe("comparison view", () => {
  it("formats rates and terms", () => {
    expect(itemValue("tax_rate", "0.0825")).toBe("8.25%");
    expect(itemValue("payment_terms", "45")).toBe("45 days");
    expect(itemValue("unit_price", "9.41")).toBe("9.41");
    expect(itemValue("unit_price", null)).toBe("—");
  });

  const comparison: Comparison = {
    id: COMPARISON_ID,
    comparison_type: "INVOICE_PO",
    origin: "AUTO",
    subject_document_id: DOC_ID,
    summary: { MATCH: 1, MISMATCH: 1, MISSING: 1, UNCERTAIN: 0 },
    created_at: "2026-10-08T10:00:00Z",
    requested_by: null,
    documents: [
      {
        document_id: DOC_ID,
        role: "INVOICE",
        position: 0,
        document_version_id: null,
        extraction_id: null,
        display_filename: "invoice-1001.pdf",
        document_type: "INVOICE",
      },
      {
        document_id: PO_ID,
        role: "PURCHASE_ORDER",
        position: 1,
        document_version_id: null,
        extraction_id: null,
        display_filename: "po-4410.pdf",
        document_type: "PURCHASE_ORDER",
      },
    ],
    settings: { price_abs: "0.01", price_pct: "0", quantity_abs: "0", min_confidence: "0.85" },
    items: [
      {
        id: "item-vendor",
        position: 0,
        item_key: "header:vendor",
        category: "HEADER",
        check_name: "vendor",
        line_key: null,
        status: "MATCH",
        left_value: "Kestrel Industrial Supply Inc.",
        right_value: "Kestrel Industrial Supply Inc.",
        difference: null,
        tolerance: null,
        explanation: "Both documents name Kestrel Industrial Supply Inc.",
        left: [side({ field_id: "fld-vendor", source_text: "KESTREL INDUSTRIAL SUPPLY" })],
        right: [side({ role: "PURCHASE_ORDER", document_id: PO_ID, document_label: "po-4410.pdf", field_id: "fld-po-vendor" })],
      },
      {
        id: "item-ws4",
        position: 2,
        item_key: "line:WS-4:line_fulfilled",
        category: "LINE_ITEM",
        check_name: "line_fulfilled",
        line_key: "WS-4",
        status: "MISSING",
        left_value: null,
        right_value: "10",
        difference: null,
        tolerance: null,
        explanation: "WS-4 is on the order but not on the invoice.",
        left: [],
        right: [side({ role: "PURCHASE_ORDER", document_id: PO_ID, document_label: "po-4410.pdf", field_id: "fld-po-ws4" })],
      },
      {
        id: "item-hb10",
        position: 1,
        item_key: "line:HB-10:unit_price",
        category: "LINE_ITEM",
        check_name: "unit_price",
        line_key: "HB-10",
        status: "MISMATCH",
        left_value: "2.65",
        right_value: "2.50",
        difference: { abs: "0.15" },
        tolerance: { abs: "0.01" },
        explanation: "Invoice unit price 2.65 USD differs from the order's 2.50 USD by 0.15 (tolerance 0.01).",
        left: [side({ field_id: "fld-hb10-price", source_text: "HB-10 Hex bolt M10 27 2.65 71.55" })],
        right: [
          side({ role: "PURCHASE_ORDER", document_id: PO_ID, document_label: "po-4410.pdf", field_id: "fld-po-hb10-price" }),
        ],
      },
    ],
  };

  it("shows each check with its result, explanation and links to the evidence", async () => {
    const user = signedIn();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(user),
      [`/api/v1/comparisons/${COMPARISON_ID}`]: () => jsonResponse(comparison),
    });
    const actor = userEvent.setup();
    renderApp(`/comparisons/${COMPARISON_ID}`);

    expect(await screen.findByRole("heading", { name: "Invoice ↔ purchase order" })).toBeInTheDocument();
    expect(screen.getByText("1 mismatch")).toBeInTheDocument();
    expect(screen.getByText(/price ±0.01 or 0%, quantity ±0/)).toBeInTheDocument();

    const lines = screen.getByRole("region", { name: "Line items" });
    const rows = within(lines).getAllByRole("row");
    // Mismatches first, and their evidence is open without a click.
    expect(rows[1]).toHaveTextContent("HB-10");
    expect(within(lines).getByText(/differs from the order's 2.50 USD/)).toBeInTheDocument();
    const links = within(lines).getAllByRole("link", { name: "Show in document" });
    expect(links.map((link) => link.getAttribute("href"))).toContain(`/documents/${DOC_ID}?field=fld-hb10-price`);
    expect(links.map((link) => link.getAttribute("href"))).toContain(`/documents/${PO_ID}?field=fld-po-hb10-price`);

    // Matches stay collapsed until asked for.
    const header = screen.getByRole("region", { name: "Header" });
    expect(within(header).queryByText(/Both documents name/)).not.toBeInTheDocument();
    await actor.click(within(header).getByRole("button", { name: "Evidence for Vendor" }));
    expect(within(header).getByText(/Both documents name/)).toBeInTheDocument();
    expect(within(header).getByText("KESTREL INDUSTRIAL SUPPLY")).toBeInTheDocument();
  });

  it("reports a comparison that is not visible", async () => {
    const user = signedIn();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(user),
      [`/api/v1/comparisons/${COMPARISON_ID}`]: () => problem(404, "Comparison not found."),
    });
    renderApp(`/comparisons/${COMPARISON_ID}`);
    expect(await screen.findByRole("alert")).toHaveTextContent("Comparison not found.");
  });
});

describe("business rules", () => {
  const rule: Rule = {
    id: "3e4f5a6b-0000-4000-8000-0000000000cc",
    code: "INV_PO_UNIT_PRICE",
    rule_type: "line_check",
    name: "Invoice unit prices match the order",
    description: "Each billed unit price must equal the ordered price within the tolerance.",
    applies_to: ["INVOICE"],
    params: { check: "unit_price", tolerance: "0.01" },
    severity: "HIGH",
    is_enabled: true,
    version: 1,
    updated_by: null,
    updated_at: "2026-10-08T09:00:00Z",
    params_schema: {},
  };

  it("lists rules read-only for viewers", async () => {
    const viewer = signedIn(VIEWER);
    mockFetch({ "/api/v1/auth/me": () => jsonResponse(viewer), "/api/v1/rules": () => jsonResponse([rule]) });
    renderApp("/rules");
    const item = await screen.findByRole("listitem", { name: "INV_PO_UNIT_PRICE" });
    expect(within(item).getByText("Invoice unit prices match the order")).toBeInTheDocument();
    expect(within(item).getByText(/High · Invoice · version 1/)).toBeInTheDocument();
    expect(within(item).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });

  it("lets a manager change parameters with a reason, validating the JSON first", async () => {
    const manager = signedIn([...ANALYST, "rules:manage"]);
    let current = rule;
    const fetchMock = mockFetch({
      "/api/v1/auth/me": () => jsonResponse(manager),
      "/api/v1/rules": () => jsonResponse([current]),
      "/api/v1/rules/INV_PO_UNIT_PRICE": (_url, init) => {
        const body = JSON.parse(init?.body as string) as { params: Rule["params"] };
        if (body.params.tolerance === "-1") return problem(422, "tolerance: must be zero or more");
        current = { ...rule, params: body.params, version: 2, updated_by: ME };
        return jsonResponse(current);
      },
    });
    const actor = userEvent.setup();
    renderApp("/rules");

    const item = await screen.findByRole("listitem", { name: "INV_PO_UNIT_PRICE" });
    await actor.click(within(item).getByRole("button", { name: "Edit" }));
    const form = within(item).getByRole("form", { name: "Edit INV_PO_UNIT_PRICE" });
    const params = within(form).getByLabelText("Parameters (JSON)");

    await actor.clear(params);
    await actor.type(params, "{{not json");
    await actor.click(within(form).getByRole("button", { name: "Save" }));
    expect(within(form).getByRole("alert")).toHaveTextContent("Parameters must be valid JSON.");
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(false);

    await actor.clear(params);
    await actor.type(params, '{{"check": "unit_price", "tolerance": "-1"}');
    await actor.click(within(form).getByRole("button", { name: "Save" }));
    expect(await within(form).findByText("tolerance: must be zero or more")).toBeInTheDocument();

    await actor.clear(params);
    await actor.type(params, '{{"check": "unit_price", "tolerance": "0.05"}');
    await actor.type(within(form).getByLabelText("Reason for the change"), "Agreed with supplier");
    await actor.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.queryByRole("form", { name: "Edit INV_PO_UNIT_PRICE" })).not.toBeInTheDocument();
    });
    const patches = fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(patches.at(-1)?.[1]?.body as string)).toEqual({
      params: { check: "unit_price", tolerance: "0.05" },
      severity: "HIGH",
      is_enabled: true,
      note: "Agreed with supplier",
    });
    expect(await screen.findByText(/version 2 · changed by Finance Analyst/)).toBeInTheDocument();
  });
});

describe("document findings", () => {
  const findings = (open: boolean): Findings => ({
    comparisons: [
      {
        id: COMPARISON_ID,
        comparison_type: "INVOICE_PO",
        origin: "AUTO",
        subject_document_id: DOC_ID,
        summary: { MATCH: 6, MISMATCH: 1, MISSING: 0, UNCERTAIN: 0 },
        created_at: "2026-10-08T10:00:00Z",
        requested_by: null,
        documents: [
          {
            document_id: DOC_ID,
            role: "INVOICE",
            position: 0,
            document_version_id: null,
            extraction_id: null,
            display_filename: "invoice-1001.pdf",
            document_type: "INVOICE",
          },
          {
            document_id: PO_ID,
            role: "PURCHASE_ORDER",
            position: 1,
            document_version_id: null,
            extraction_id: null,
            display_filename: "po-4410.pdf",
            document_type: "PURCHASE_ORDER",
          },
        ],
      },
    ],
    rule_results: [
      {
        id: "r-pass",
        rule_code: "INV_PO_VENDOR",
        rule_version: 1,
        outcome: "PASS",
        severity: "CRITICAL",
        message: "The vendor matches the order.",
        evidence: {},
        items: [],
        comparison_id: COMPARISON_ID,
        evaluated_at: "2026-10-08T10:00:00Z",
      },
      {
        id: "r-warn",
        rule_code: "DOC_UNKNOWN_VENDOR",
        rule_version: 1,
        outcome: "WARN",
        severity: "MEDIUM",
        message: "The vendor is not in the vendor list.",
        evidence: {},
        items: [],
        comparison_id: null,
        evaluated_at: "2026-10-08T10:00:00Z",
      },
      {
        id: "r-fail",
        rule_code: "INV_PO_UNIT_PRICE",
        rule_version: 1,
        outcome: "FAIL",
        severity: "HIGH",
        message: "Unit price of HB-10 is 2.65 USD on the invoice but 2.50 USD on the order.",
        evidence: {},
        items: ["line:HB-10:unit_price"],
        comparison_id: COMPARISON_ID,
        evaluated_at: "2026-10-08T10:00:00Z",
      },
      {
        id: "r-na",
        rule_code: "INV_DELIVERED_QUANTITY",
        rule_version: 1,
        outcome: "NOT_APPLICABLE",
        severity: "HIGH",
        message: "No delivery note is on file for this order.",
        evidence: {},
        items: [],
        comparison_id: null,
        evaluated_at: "2026-10-08T10:00:00Z",
      },
    ],
    duplicates: [
      {
        kind: "SAME_VENDOR_AND_NUMBER",
        document_id: COPY_ID,
        display_filename: "invoice-1001-resent.pdf",
        direction: "copy",
        evidence: {},
      },
    ],
    open_task: open ? task() : null,
    review_history: open
      ? []
      : [task({ status: "RESOLVED", resolution: "APPROVED", resolved_by: ME, resolved_at: "2026-10-08T11:00:00Z" })],
  });

  function routes(state: { open: boolean }) {
    return {
      [DETAIL_URL]: () =>
        jsonResponse(
          detail(
            state.open
              ? {
                  status: "REVIEW_REQUIRED",
                  review_reasons: ["RULE_VIOLATION", "DUPLICATE_SUSPECTED"],
                  review: task(),
                }
              : {},
          ),
        ),
      [`${DETAIL_URL}/extraction`]: () => problem(404, "No extraction for this document."),
      [FINDINGS_URL]: () => jsonResponse(findings(state.open)),
      [VERSIONS_URL]: () => jsonResponse([]),
    };
  }

  it("shows the open review, rule results, comparisons and duplicates", async () => {
    const user = signedIn();
    mockFetch({ "/api/v1/auth/me": () => jsonResponse(user), ...routes({ open: true }) });
    renderApp(`/documents/${DOC_ID}`);

    const region = await screen.findByRole("region", { name: "Checks and review" });
    const banner = screen.getAllByRole("status").find((element) => element.textContent.startsWith("Needs review"));
    expect(banner).toBeDefined();
    expect(banner).toHaveTextContent("High");
    expect(banner).toHaveTextContent("A business rule failed or could not be verified");
    expect(banner).toHaveTextContent("It may duplicate another document");

    const card = within(region).getByRole("group", { name: "Open review: Discrepancy" });
    expect(within(card).getByText(/Unit price of HB-10 is 2.65 USD on the invoice/)).toBeInTheDocument();
    expect(within(card).getByText(/unassigned/)).toBeInTheDocument();

    const attention = within(region).getByRole("list", { name: "Rules needing attention" });
    const items = within(attention).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining("INV_PO_UNIT_PRICE"),
      expect.stringContaining("DOC_UNKNOWN_VENDOR"),
    ]);
    expect(within(items[0] as HTMLElement).getByText("Failed")).toBeInTheDocument();
    expect(within(items[0] as HTMLElement).getByRole("link", { name: "view comparison" })).toHaveAttribute(
      "href",
      `/comparisons/${COMPARISON_ID}`,
    );
    expect(within(region).getByText("1 passed, 1 not applicable")).toBeInTheDocument();

    expect(within(region).getByRole("link", { name: "Invoice ↔ purchase order" })).toHaveAttribute(
      "href",
      `/comparisons/${COMPARISON_ID}`,
    );
    expect(within(region).getByText("1 mismatch")).toBeInTheDocument();
    expect(within(region).getByText(/Purchase order: po-4410.pdf/)).toBeInTheDocument();
    expect(within(region).getByRole("link", { name: "invoice-1001-resent.pdf" })).toHaveAttribute(
      "href",
      `/documents/${COPY_ID}`,
    );
    expect(within(region).getByText(/Same vendor and document number/)).toBeInTheDocument();
  });

  it("requires a note to reject, then records an approval", async () => {
    const user = signedIn();
    const state = { open: true };
    const fetchMock = mockFetch({
      "/api/v1/auth/me": () => jsonResponse(user),
      ...routes(state),
      "/api/v1/review-tasks?state=open&assigned=any&limit=25&offset=0": () =>
        jsonResponse({ items: [], total: 0, limit: 25, offset: 0 }),
      [`/api/v1/review-tasks/${TASK_ID}/resolve`]: () => {
        state.open = false;
        return jsonResponse(listItem({ status: "RESOLVED", resolution: "APPROVED", resolved_by: ME }));
      },
    });
    const actor = userEvent.setup();
    renderApp(`/documents/${DOC_ID}`);

    const form = await screen.findByRole("form", { name: "Resolve review" });
    await actor.selectOptions(within(form).getByLabelText("Decision"), "REJECTED");
    await actor.click(within(form).getByRole("button", { name: "Resolve" }));
    expect(await screen.findByText("A rejection needs a note explaining why.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => typeof url === "string" && url.endsWith("/resolve"))).toBe(false);

    await actor.selectOptions(within(form).getByLabelText("Decision"), "APPROVED");
    await actor.click(within(form).getByRole("button", { name: "Resolve" }));

    const region = screen.getByRole("region", { name: "Checks and review" });
    expect(await within(region).findByText(/approved by Finance Analyst/)).toBeInTheDocument();
    expect(within(region).queryByRole("group", { name: /Open review/ })).not.toBeInTheDocument();
    const resolve = fetchMock.mock.calls.find(([url]) => url === `/api/v1/review-tasks/${TASK_ID}/resolve`);
    expect(resolve?.[1]?.method).toBe("POST");
    expect(JSON.parse(resolve?.[1]?.body as string)).toEqual({ resolution: "APPROVED", note: null });
    await waitFor(() => {
      expect(screen.queryByText("Needs review")).not.toBeInTheDocument();
    });
  });

  it("hides review actions from users who do not work the queue", async () => {
    const viewer = signedIn(VIEWER);
    mockFetch({ "/api/v1/auth/me": () => jsonResponse(viewer), ...routes({ open: true }) });
    renderApp(`/documents/${DOC_ID}`);
    const region = await screen.findByRole("region", { name: "Checks and review" });
    expect(within(region).getByRole("group", { name: "Open review: Discrepancy" })).toBeInTheDocument();
    expect(within(region).queryByRole("form", { name: "Resolve review" })).not.toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: "Claim" })).not.toBeInTheDocument();
  });

  it("shows the review priority in the documents inbox", async () => {
    const user = signedIn();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(user),
      [documentsUrl({ status: "", q: "", offset: 0 })]: () =>
        jsonResponse({
          items: [summary({ status: "REVIEW_REQUIRED", review: { ...task(), priority: "URGENT" } })],
          total: 1,
          limit: 25,
          offset: 0,
        }),
    });
    renderApp("/documents");
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Needs review")).toBeInTheDocument();
    expect(within(table).getByText("Urgent")).toBeInTheDocument();
  });
});

describe("document versions", () => {
  const versions: VersionInfo[] = [
    {
      id: "v2",
      version_number: 2,
      original_filename: "msa-v2.pdf",
      mime_type: "application/pdf",
      size_bytes: 5120,
      sha256: "b".repeat(64),
      page_count: 2,
      created_at: "2026-10-08T12:00:00Z",
      processed: true,
      is_current: true,
    },
    {
      id: "v1",
      version_number: 1,
      original_filename: "msa-v1.pdf",
      mime_type: "application/pdf",
      size_bytes: 4096,
      sha256: "a".repeat(64),
      page_count: 2,
      created_at: "2026-10-08T09:59:00Z",
      processed: true,
      is_current: false,
    },
  ];
  const diff: VersionComparison = {
    document_id: DOC_ID,
    from_version: 1,
    to_version: 2,
    summary: { MODIFIED: 1, ADDED: 1, REMOVED: 0, UNCHANGED: 3 },
    clauses: [
      {
        change: "MODIFIED",
        title: "Payment Terms",
        old: { key: "4", number: "4", title: "Payment Terms", text: "Invoices are payable within 30 days.", page: 1 },
        new: { key: "4", number: "4", title: "Payment Terms", text: "Invoices are payable within 45 days.", page: 1 },
        similarity: 0.93,
        renumbered: false,
        operations: [
          { op: "equal", old: "Invoices are payable within", new: "Invoices are payable within" },
          { op: "replace", old: "30", new: "45" },
          { op: "equal", old: "days.", new: "days." },
        ],
      },
      {
        change: "ADDED",
        title: "Data Protection",
        old: null,
        new: { key: "9", number: "9", title: "Data Protection", text: "Personal data is processed under the DPA.", page: 2 },
        similarity: 0,
        renumbered: false,
        operations: [],
      },
      {
        change: "UNCHANGED",
        title: "Term",
        old: { key: "2", number: "2", title: "Term", text: "Two years.", page: 1 },
        new: { key: "2", number: "2", title: "Term", text: "Two years.", page: 1 },
        similarity: 1,
        renumbered: false,
        operations: [],
      },
    ],
  };

  function routes() {
    return {
      [DETAIL_URL]: () => jsonResponse(detail({ document_type: "CONTRACT", display_filename: "msa.pdf" })),
      [`${DETAIL_URL}/extraction`]: () => problem(404, "No extraction for this document."),
      [FINDINGS_URL]: () =>
        jsonResponse({ comparisons: [], rule_results: [], duplicates: [], open_task: null, review_history: [] }),
      [VERSIONS_URL]: () => jsonResponse(versions),
      [`${VERSIONS_URL}/compare?from=1&to=2`]: () => jsonResponse(diff),
    };
  }

  it("lists versions and shows clause-level changes between two of them", async () => {
    const user = signedIn();
    const fetchMock = mockFetch({ "/api/v1/auth/me": () => jsonResponse(user), ...routes() });
    const actor = userEvent.setup();
    renderApp(`/documents/${DOC_ID}`);

    const region = await screen.findByRole("region", { name: "Versions" });
    const rows = within(region).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("2current");
    expect(rows[2]).toHaveTextContent("msa-v1.pdf");
    expect(within(region).getByLabelText("From")).toHaveValue("1");
    expect(within(region).getByLabelText("To")).toHaveValue("2");

    await actor.click(within(region).getByRole("button", { name: "Compare" }));
    const changes = await within(region).findByLabelText("Changes from version 1 to 2");
    expect(fetchMock.mock.calls.some(([url]) => url === `${VERSIONS_URL}/compare?from=1&to=2`)).toBe(true);
    expect(within(changes).getByText("1 modified")).toBeInTheDocument();
    const payment = within(changes).getByRole("listitem", { name: "4 Payment Terms" });
    expect(payment.querySelector("del")).toHaveTextContent("30");
    expect(payment.querySelector("ins")).toHaveTextContent("45");
    const added = within(changes).getByRole("listitem", { name: "9 Data Protection" });
    expect(within(added).getByText("Personal data is processed under the DPA.")).toBeInTheDocument();
    expect(within(changes).queryByRole("listitem", { name: "2 Term" })).not.toBeInTheDocument();
    expect(within(changes).getByText("1 clause(s) unchanged.")).toBeInTheDocument();
  });

  it("uploads a new version as multipart form data and explains a refusal", async () => {
    const user = signedIn();
    let attempts = 0;
    const fetchMock = mockFetch({
      "/api/v1/auth/me": () => jsonResponse(user),
      ...routes(),
      [VERSIONS_URL]: (_url, init) => {
        if (init?.method !== "POST") return jsonResponse(versions);
        attempts += 1;
        return attempts === 1
          ? problem(409, "The file is identical to the current version.")
          : jsonResponse(summary({ status: "PENDING" }), 201);
      },
    });
    const actor = userEvent.setup();
    renderApp(`/documents/${DOC_ID}`);

    const form = await screen.findByRole("form", { name: "Upload new version" });
    const file = new File(["%PDF-1.7 v3"], "msa-v3.pdf", { type: "application/pdf" });
    await actor.upload(within(form).getByLabelText("New version of this document"), file);
    await actor.click(within(form).getByRole("button", { name: "Upload version" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("identical to the current version");

    await actor.upload(within(form).getByLabelText("New version of this document"), file);
    await actor.click(within(form).getByRole("button", { name: "Upload version" }));
    expect(await within(form).findByRole("status")).toHaveTextContent("queued for processing");
    const post = fetchMock.mock.calls.filter(([url, init]) => url === VERSIONS_URL && init?.method === "POST").at(-1);
    const body = post?.[1]?.body;
    expect(body).toBeInstanceOf(FormData);
    expect(((body as FormData).get("file") as File).name).toBe("msa-v3.pdf");
  });

  it("hides the upload form from users without upload permission", async () => {
    const viewer = signedIn(VIEWER);
    mockFetch({ "/api/v1/auth/me": () => jsonResponse(viewer), ...routes() });
    renderApp(`/documents/${DOC_ID}`);
    await screen.findByRole("region", { name: "Versions" });
    expect(screen.queryByRole("form", { name: "Upload new version" })).not.toBeInTheDocument();
  });
});
