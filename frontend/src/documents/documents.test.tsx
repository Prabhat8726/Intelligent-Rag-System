import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { filenameFromDisposition } from "../lib/api";
import type {
  Classification,
  DocumentDetail,
  DocumentSummary,
  DocumentTable,
  ExtractedField,
  Extraction,
  Findings,
  Page,
  PageDetail,
  PageSummary,
} from "../lib/types";
import { browserHasSession, CURRENT_USER, jsonResponse, mockFetch, problem, renderApp } from "../test/utils";
import { documentsUrl, moneyText } from "./format";

const LIST_URL = documentsUrl({ status: "", q: "", offset: 0 });
const DOC_ID = "5b0d8a3e-6b43-4c86-9f73-0b5b0f1f0a11";
const EXTRACTION_URL = `/api/v1/documents/${DOC_ID}/extraction`;
const FINDINGS_URL = `/api/v1/documents/${DOC_ID}/findings`;
const VERSIONS_URL = `/api/v1/documents/${DOC_ID}/versions`;
const NO_FINDINGS: Findings = { comparisons: [], rule_results: [], duplicates: [], open_task: null, review_history: [] };

/** The detail page also loads findings and versions (covered in review.test.tsx): empty by default. */
function mockApi(handlers: Parameters<typeof mockFetch>[0]) {
  return mockFetch({
    [FINDINGS_URL]: () => jsonResponse(NO_FINDINGS),
    [VERSIONS_URL]: () => jsonResponse([]),
    ...handlers,
  });
}

function document(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: DOC_ID,
    display_filename: "invoice-1001.pdf",
    document_type: null,
    type_confidence: null,
    status: "COMPLETED",
    sensitivity: "INTERNAL",
    source: "UPLOAD",
    owner: { id: CURRENT_USER.id, full_name: "Finance Analyst" },
    department: CURRENT_USER.department,
    duplicate_of_id: null,
    duplicate_reason: null,
    processing_error: null,
    review_reasons: [],
    last_processed_at: "2026-10-08T10:00:00Z",
    created_at: "2026-10-08T09:59:00Z",
    updated_at: "2026-10-08T10:00:00Z",
    current_version: {
      id: "c3b6c0a2-0000-4000-8000-000000000001",
      version_number: 1,
      original_filename: "invoice-1001.pdf",
      mime_type: "application/pdf",
      size_bytes: 3456,
      sha256: "a".repeat(64),
      page_count: 2,
      created_at: "2026-10-08T09:59:00Z",
    },
    ...overrides,
  };
}

function page(items: DocumentSummary[]): Page<DocumentSummary> {
  return { items, total: items.length, limit: 25, offset: 0 };
}

// The analyst role's document permissions (see backend/src/docintel/auth/permissions.py).
const ANALYST_PERMISSIONS = ["documents:read", "documents:upload", "documents:process", "documents:review"];

function signedIn(permissions: string[] = ANALYST_PERMISSIONS) {
  browserHasSession();
  return { ...CURRENT_USER, permissions };
}

describe("documents inbox", () => {
  it("lists documents with status, pages and duplicate flag", async () => {
    const user = signedIn();
    mockApi({
      "/api/v1/auth/me": () => jsonResponse(user),
      [LIST_URL]: () =>
        jsonResponse(
          page([
            document({ extraction: { overall_confidence: "0.9420", review_level: "AUTO" } }),
            document({ id: "d2", display_filename: "scan.tiff", status: "PROCESSING", duplicate_of_id: DOC_ID }),
          ]),
        ),
    });
    renderApp("/documents");

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(within(rows[1] as HTMLElement).getByText("Processed")).toBeInTheDocument();
    // The current extraction's confidence and routing; nothing yet while processing.
    expect(within(rows[1] as HTMLElement).getByText("94%")).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText("Auto-accepted")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).queryByText("Auto-accepted")).not.toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("Processing")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("duplicate")).toBeInTheDocument();
    expect(screen.getByText("1–2 of 2")).toBeInTheDocument();
  });

  it("uploads a file as multipart form data and refreshes the list", async () => {
    const user = signedIn();
    let listCalls = 0;
    const fetchMock = mockApi({
      "/api/v1/auth/me": () => jsonResponse(user),
      [LIST_URL]: () => {
        listCalls += 1;
        return jsonResponse(page(listCalls > 1 ? [document({ status: "PENDING" })] : []));
      },
      "/api/v1/documents": () => jsonResponse(document({ status: "PENDING" }), 201),
    });
    const actor = userEvent.setup();
    renderApp("/documents");

    await screen.findByText("No documents match.");
    const file = new File(["%PDF-1.7 test"], "invoice-1001.pdf", { type: "application/pdf" });
    await actor.upload(screen.getByLabelText(/Document \(PDF/), file);
    await actor.selectOptions(screen.getByLabelText("Sensitivity"), "CONFIDENTIAL");
    await actor.click(screen.getByRole("button", { name: "Upload" }));

    expect(await screen.findByRole("status")).toHaveTextContent("queued for processing");
    const upload = fetchMock.mock.calls.find(([url, init]) => url === "/api/v1/documents" && init?.method === "POST");
    const body = upload?.[1]?.body;
    expect(body).toBeInstanceOf(FormData);
    expect((body as FormData).get("sensitivity")).toBe("CONFIDENTIAL");
    expect(((body as FormData).get("file") as File).name).toBe("invoice-1001.pdf");
    expect(upload?.[1]?.headers).not.toHaveProperty("Content-Type");
    await waitFor(() => {
      expect(listCalls).toBeGreaterThan(1);
    });
  });

  it("shows the server's reason when an upload is rejected", async () => {
    const user = signedIn();
    mockApi({
      "/api/v1/auth/me": () => jsonResponse(user),
      [LIST_URL]: () => jsonResponse(page([])),
      "/api/v1/documents": () => problem(415, "The file extension does not match the file content."),
    });
    const actor = userEvent.setup();
    renderApp("/documents");

    await actor.upload(
      await screen.findByLabelText(/Document \(PDF/),
      new File(["<html>"], "invoice.pdf", { type: "application/pdf" }),
    );
    await actor.click(screen.getByRole("button", { name: "Upload" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("does not match the file content");
  });

  it("hides the upload form from users without upload permission", async () => {
    const viewer = signedIn(["documents:read"]);
    mockApi({
      "/api/v1/auth/me": () => jsonResponse(viewer),
      [LIST_URL]: () => jsonResponse(page([document()])),
    });
    renderApp("/documents");
    await screen.findByRole("table");
    expect(screen.queryByRole("form", { name: "Upload document" })).not.toBeInTheDocument();
  });
});

describe("document detail", () => {
  const detail: DocumentDetail = {
    ...document(),
    inspection: {
      kind: "mixed_pdf",
      page_count: 2,
      pages_needing_ocr: [2],
      version: 1,
      pages: [
        { page_number: 1, width: 595.28, height: 841.89, unit: "pt", rotation: 0, text_chars: 420, image_objects: 0, method: "NATIVE", dpi: null },
        { page_number: 2, width: 595.28, height: 841.89, unit: "pt", rotation: 0, text_chars: 0, image_objects: 1, method: "OCR", dpi: null },
      ],
    },
    sensitivity_assessment: null,
    classification: null,
    classification_history: [],
    pages: [],
    latest_job: {
      id: "job-1",
      job_type: "DOCUMENT_PROCESSING",
      status: "COMPLETED",
      attempts: 1,
      max_attempts: 3,
      stage: "done",
      stage_timings: { integrity: 4.2, inspect: 31.7 },
      last_error: null,
      created_at: "2026-10-08T09:59:00Z",
      started_at: "2026-10-08T09:59:01Z",
      finished_at: "2026-10-08T09:59:01Z",
      duration_ms: 48,
    },
  };

  it("shows file facts, processing timings and per-page inspection", async () => {
    const user = signedIn();
    mockApi({
      "/api/v1/auth/me": () => jsonResponse(user),
      [`/api/v1/documents/${DOC_ID}`]: () => jsonResponse(detail),
      [EXTRACTION_URL]: () => problem(404, "No extraction for this document."),
    });
    renderApp(`/documents/${DOC_ID}`);

    expect(await screen.findByRole("heading", { name: "invoice-1001.pdf" })).toBeInTheDocument();
    expect(screen.getByText("Mixed PDF (some pages need OCR) · OCR needed on 1 of 2 page(s)")).toBeInTheDocument();
    expect(screen.getByText("Needs OCR")).toBeInTheDocument();
    expect(screen.getByText("Text layer")).toBeInTheDocument();
    expect(screen.getByText("48 ms")).toBeInTheDocument();
    expect(screen.getByText("Stage: inspect")).toBeInTheDocument();
    expect(screen.getByText("a".repeat(64))).toBeInTheDocument();
    // ANALYST: may reprocess, may not delete.
    expect(screen.getByRole("button", { name: "Reprocess" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("requests reprocessing and reports conflicts", async () => {
    const user = signedIn();
    const fetchMock = mockApi({
      "/api/v1/auth/me": () => jsonResponse(user),
      [`/api/v1/documents/${DOC_ID}`]: () => jsonResponse(detail),
      [EXTRACTION_URL]: () => problem(404, "No extraction for this document."),
      [`/api/v1/documents/${DOC_ID}/process`]: () =>
        problem(409, "The document is already queued or being processed."),
    });
    const actor = userEvent.setup();
    renderApp(`/documents/${DOC_ID}`);

    await actor.click(await screen.findByRole("button", { name: "Reprocess" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already queued");
    expect(
      fetchMock.mock.calls.some(([url, init]) => url === `/api/v1/documents/${DOC_ID}/process` && init?.method === "POST"),
    ).toBe(true);
  });

  it("shows not found for inaccessible documents", async () => {
    const user = signedIn();
    mockApi({
      "/api/v1/auth/me": () => jsonResponse(user),
      [`/api/v1/documents/${DOC_ID}`]: () => problem(404, "Document not found."),
    });
    renderApp(`/documents/${DOC_ID}`);
    expect(await screen.findByText("Document not found.")).toBeInTheDocument();
  });
});

describe("moneyText", () => {
  it("pads to the currency's minor units without rounding", () => {
    expect(moneyText("125.3", "USD")).toBe("125.30");
    expect(moneyText("1200", "EUR")).toBe("1200.00");
    expect(moneyText("0.125", "USD")).toBe("0.125");
    expect(moneyText("1200", "JPY")).toBe("1200");
    expect(moneyText("-4.5", "GBP")).toBe("-4.50");
  });
});

describe("filenameFromDisposition", () => {
  it("prefers the UTF-8 filename and falls back to the ASCII one", () => {
    expect(filenameFromDisposition(`attachment; filename="Rechnung M_rz.pdf"; filename*=UTF-8''Rechnung%20M%C3%A4rz.pdf`)).toBe(
      "Rechnung März.pdf",
    );
    expect(filenameFromDisposition('attachment; filename="plain.pdf"')).toBe("plain.pdf");
    expect(filenameFromDisposition(null)).toBeNull();
  });
});

const ANALYST_WITH_REVIEW = [...ANALYST_PERMISSIONS];

describe("document understanding", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const machine: Classification = {
    id: "cls-1",
    label: "INVOICE",
    confidence: "0.6200",
    method: "LOCAL_MODEL",
    model_version: "tfidf-lr-v1:abc",
    signals: {
      local: [
        { label: "INVOICE", probability: 0.62 },
        { label: "RECEIPT", probability: 0.3 },
      ],
      keywords: { INVOICE: ["invoice", "total due"] },
      llm: { used: false, reason: "no external AI provider configured" },
    },
    note: null,
    created_by: null,
    is_current: true,
    created_at: "2026-10-08T10:00:00Z",
  };
  const firstPage: PageSummary = {
    page_number: 1,
    width: 595,
    height: 842,
    unit: "pt",
    rotation_applied: 0,
    extraction_method: "OCR",
    ocr_confidence: "91.40",
    word_count: 2,
    preview_width: 300,
    preview_height: 424,
    has_preview: true,
  };
  const understood: DocumentDetail = {
    ...document({ status: "REVIEW_REQUIRED", document_type: "INVOICE", type_confidence: "0.6200" }),
    review_reasons: ["CLASSIFICATION_UNCERTAIN"],
    inspection: null,
    latest_job: null,
    sensitivity_assessment: { findings: [], type_minimum: null, detected: null },
    classification: machine,
    classification_history: [machine],
    pages: [firstPage],
  };
  const pageDetail: PageDetail = {
    ...firstPage,
    text: "INVOICE\n\nTotal Due  71.55",
    words: [
      ["INVOICE", 72, 60, 160, 80, 95.5, 20],
      ["71.55", 300, 400, 340, 412, 42.0, 11],
    ],
    layout: { lines: [], blocks: [], warnings: [] },
  };
  const tables: DocumentTable[] = [
    {
      id: "tbl-1",
      table_index: 0,
      page_start: 1,
      page_end: 2,
      header: ["#", "Item", "Amount"],
      bbox: [0, 0, 1, 1],
      extraction_method: "NATIVE",
      confidence: "1.0000",
      row_count: 1,
      rows: [{ row_index: 0, page_number: 1, cells: ["1", "BLT-M10", "71.55"], bbox: [0, 0, 1, 1] }],
    },
  ];

  function understandingRoutes(detail: DocumentDetail = understood) {
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:page-1", revokeObjectURL: () => undefined }));
    return {
      [`/api/v1/documents/${DOC_ID}`]: () => jsonResponse(detail),
      [`/api/v1/documents/${DOC_ID}/pages/1`]: () => jsonResponse(pageDetail),
      [`/api/v1/documents/${DOC_ID}/pages/1/image`]: () =>
        new Response(new Uint8Array([137, 80, 78, 71]), { headers: { "content-type": "image/png" } }),
      [`/api/v1/documents/${DOC_ID}/tables`]: () => jsonResponse(tables),
      [EXTRACTION_URL]: () => problem(404, "No extraction for this document."),
    };
  }

  it("shows type, evidence, review reasons, page text and preview, and tables", async () => {
    const user = signedIn();
    mockApi({ "/api/v1/auth/me": () => jsonResponse(user), ...understandingRoutes() });
    renderApp(`/documents/${DOC_ID}`);

    const card = await screen.findByRole("region", { name: "Document type" });
    expect(within(card).getByText("Invoice", { selector: "span.text-lg" })).toBeInTheDocument();
    expect(within(card).getByText("62% confidence")).toBeInTheDocument();
    expect(within(card).getByText("invoice, total due")).toBeInTheDocument();
    expect(within(card).getByText(/Not used: no external AI provider configured/)).toBeInTheDocument();
    expect(screen.getByText("Document type is uncertain")).toBeInTheDocument();

    expect(await screen.findByText(/Total Due 71\.55/)).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Page 1 preview" })).toHaveAttribute("src", "blob:page-1");
    expect(screen.getByText(/OCR · confidence 91%/)).toBeInTheDocument();

    const tablesRegion = screen.getByRole("region", { name: "Tables" });
    expect(await within(tablesRegion).findByText("BLT-M10")).toBeInTheDocument();
    expect(within(tablesRegion).getByText(/pages 1–2/)).toBeInTheDocument();
  });

  it("lets a reviewer correct the document type", async () => {
    const user = signedIn(ANALYST_WITH_REVIEW);
    const corrected: Classification = {
      ...machine,
      id: "cls-2",
      label: "RECEIPT",
      confidence: "1.0000",
      method: "HUMAN",
      created_by: { id: user.id, full_name: "Finance Analyst" },
      note: "till receipt",
    };
    let current: DocumentDetail = understood;
    const routes = understandingRoutes();
    const fetchMock = mockApi({
      "/api/v1/auth/me": () => jsonResponse(user),
      ...routes,
      [`/api/v1/documents/${DOC_ID}`]: () => jsonResponse(current),
      [`/api/v1/documents/${DOC_ID}/classification`]: () => {
        current = {
          ...understood,
          status: "COMPLETED",
          review_reasons: [],
          document_type: "RECEIPT",
          classification: corrected,
          classification_history: [corrected, { ...machine, is_current: false }],
        };
        return jsonResponse(corrected);
      },
    });
    const actor = userEvent.setup();
    renderApp(`/documents/${DOC_ID}`);

    await actor.click(await screen.findByRole("button", { name: "Correct type" }));
    const form = screen.getByRole("form", { name: "Correct document type" });
    await actor.selectOptions(within(form).getByLabelText("Document type"), "RECEIPT");
    await actor.type(within(form).getByLabelText("Note (optional)"), "till receipt");
    await actor.click(within(form).getByRole("button", { name: "Save" }));

    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(patch?.[1]?.body as string)).toEqual({ document_type: "RECEIPT", note: "till receipt" });
    const card = screen.getByRole("region", { name: "Document type" });
    expect(await within(card).findByText("Receipt", { selector: "span.text-lg" })).toBeInTheDocument();
    expect(within(card).getByText(/Human · Finance Analyst/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("Document type is uncertain")).not.toBeInTheDocument();
    });
  });

  it("hides the correction from users without review permission", async () => {
    const viewer = signedIn(["documents:read"]);
    mockApi({ "/api/v1/auth/me": () => jsonResponse(viewer), ...understandingRoutes() });
    renderApp(`/documents/${DOC_ID}`);
    await screen.findByRole("region", { name: "Document type" });
    expect(screen.queryByRole("button", { name: "Correct type" })).not.toBeInTheDocument();
  });

  it("shows the type in the inbox and filters by it", async () => {
    const user = signedIn();
    const typed = document({ document_type: "INVOICE", type_confidence: "0.9600" });
    const fetchMock = mockApi({
      "/api/v1/auth/me": () => jsonResponse(user),
      [LIST_URL]: () => jsonResponse(page([typed])),
      [documentsUrl({ status: "", type: "INVOICE", q: "", offset: 0 })]: () => jsonResponse(page([typed])),
    });
    const actor = userEvent.setup();
    renderApp("/documents");
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Invoice")).toBeInTheDocument();
    expect(within(table).getByText("96%")).toBeInTheDocument();
    await actor.selectOptions(screen.getByLabelText("Filter by type"), "INVOICE");
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) => typeof url === "string" && url.includes("document_type=INVOICE")),
      ).toBe(true);
    });
  });
});

describe("extracted data", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const KESTREL = { id: "0e7c1c3a-5f0e-4d1b-9a55-6c1e2b3d4f50", canonical_name: "Kestrel Industrial Supply Inc." };

  function field(overrides: Partial<ExtractedField> & Pick<ExtractedField, "field_path">): ExtractedField {
    return {
      id: `fld-${overrides.field_path}`,
      field_name: overrides.field_path,
      group_name: null,
      row_index: null,
      value_type: "TEXT",
      is_required: true,
      original_value: null,
      normalized_value: null,
      page_number: 1,
      source_text: null,
      bbox: null,
      evidence_status: "VERIFIED",
      origin: "LOCAL",
      method: "label_same_line",
      confidence: "0.9800",
      confidence_signals: {},
      alternatives: [],
      corrected_value: null,
      corrected_normalized: null,
      correction_note: null,
      corrected_by: null,
      corrected_at: null,
      ...overrides,
    };
  }

  const fields: ExtractedField[] = [
    field({
      field_path: "vendor_name",
      original_value: "KESTREL INDUSTRIAL SUPPLY",
      normalized_value: {
        value: "KESTREL INDUSTRIAL SUPPLY",
        status: "OK",
        vendor: { vendor_id: KESTREL.id, canonical_name: KESTREL.canonical_name, score: 92.5, method: "name" },
      },
      bbox: [72, 40, 300, 60],
      method: "letterhead",
    }),
    field({
      field_path: "invoice_number",
      original_value: "INV-2026-0O42",
      normalized_value: { value: "INV-2026-0O42", status: "OK" },
      evidence_status: "FUZZY",
      confidence: "0.7100",
      bbox: [380, 60, 480, 72],
    }),
    field({
      field_path: "invoice_date",
      value_type: "DATE",
      original_value: "03/04/2026",
      normalized_value: { value: "2026-03-04", status: "UNCERTAIN", alternatives: ["2026-03-04", "2026-04-03"] },
      confidence: "0.5500",
      bbox: [380, 76, 440, 88],
    }),
    field({
      field_path: "total",
      value_type: "MONEY",
      original_value: "135.64",
      normalized_value: { value: "135.64", status: "OK", currency: "USD" },
      alternatives: [{ origin: "LLM", value: "0.00", page: 1, evidence: "VERIFIED" }],
      confidence: "0.4000",
      bbox: [440, 400, 480, 412],
    }),
    field({
      field_path: "due_date",
      value_type: "DATE",
      is_required: false,
      page_number: null,
      evidence_status: "NOT_FOUND",
      origin: null,
      method: null,
      confidence: "0.0000",
    }),
    field({
      field_path: "line_items[0].description",
      field_name: "description",
      group_name: "line_items",
      row_index: 0,
      original_value: "Hex bolt M10",
      normalized_value: { value: "Hex bolt M10", status: "OK" },
      bbox: [100, 300, 200, 312],
    }),
    field({
      field_path: "line_items[0].amount",
      field_name: "amount",
      group_name: "line_items",
      row_index: 0,
      value_type: "MONEY",
      original_value: "71.55",
      normalized_value: { value: "71.55", status: "OK", currency: "USD" },
      bbox: [300, 400, 340, 412],
    }),
  ];

  const extraction: Extraction = {
    id: "ext-1",
    document_version_id: "c3b6c0a2-0000-4000-8000-000000000001",
    schema_name: "invoice",
    schema_version: 1,
    status: "SUCCEEDED",
    method: "COMBINED",
    provider: "test-provider",
    model: "test-model",
    prompt_version: "extract-v1",
    overall_confidence: "0.4000",
    review_level: "ANALYST_REVIEW",
    checks: [
      {
        code: "TOTAL_ARITHMETIC",
        status: "FAIL",
        fields: ["subtotal", "tax_amount", "total"],
        expected: "134.64",
        actual: "135.64",
        message: "Total 135.64 does not equal subtotal plus tax (134.64).",
      },
      {
        code: "LINES_SUM",
        status: "PASS",
        fields: ["line_items", "subtotal"],
        expected: "120.00",
        actual: "120.00",
        message: "Line amounts add up to the subtotal.",
      },
    ],
    signals: { llm: { mode: "auto", used: true, model: "test-model", cache_hit: false } },
    validation_error_count: 0,
    created_at: "2026-10-08T10:00:00Z",
    fields,
    vendor: KESTREL,
  };

  const firstPage: PageSummary = {
    page_number: 1,
    width: 595,
    height: 842,
    unit: "pt",
    rotation_applied: 0,
    extraction_method: "NATIVE",
    ocr_confidence: null,
    word_count: 2,
    preview_width: 300,
    preview_height: 424,
    has_preview: true,
  };
  const reviewed: DocumentDetail = {
    ...document({ status: "REVIEW_REQUIRED", document_type: "INVOICE", type_confidence: "0.9700", vendor: KESTREL }),
    review_reasons: ["EXTRACTION_INCONSISTENT"],
    inspection: null,
    latest_job: null,
    sensitivity_assessment: { findings: [], type_minimum: null, detected: null },
    classification: null,
    classification_history: [],
    pages: [firstPage],
  };

  function extractionRoutes(current: () => Extraction = () => extraction) {
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:page-1", revokeObjectURL: () => undefined }));
    return {
      [`/api/v1/documents/${DOC_ID}`]: () => jsonResponse(reviewed),
      [`/api/v1/documents/${DOC_ID}/pages/1`]: () =>
        jsonResponse({ ...firstPage, text: "INVOICE", words: [], layout: { lines: [], blocks: [], warnings: [] } }),
      [`/api/v1/documents/${DOC_ID}/pages/1/image`]: () =>
        new Response(new Uint8Array([137, 80, 78, 71]), { headers: { "content-type": "image/png" } }),
      [`/api/v1/documents/${DOC_ID}/tables`]: () => jsonResponse([]),
      [EXTRACTION_URL]: () => jsonResponse(current()),
    };
  }

  function fieldRow(label: string): HTMLElement {
    const region = screen.getByRole("region", { name: "Extracted data" });
    return within(region).getByRole("row", { name: new RegExp(`^${label}\\b`) });
  }

  it("shows values with their evidence, confidence, failed checks and review level", async () => {
    const user = signedIn();
    mockApi({ "/api/v1/auth/me": () => jsonResponse(user), ...extractionRoutes() });
    renderApp(`/documents/${DOC_ID}`);

    const region = await screen.findByRole("region", { name: "Extracted data" });
    expect(within(region).getByText("Analyst review")).toBeInTheDocument();
    expect(within(region).getByText("confidence 40%")).toBeInTheDocument();
    expect(within(region).getByText(/Layout rules \+ AI model · vendor: Kestrel Industrial Supply Inc\./)).toBeInTheDocument();
    expect(within(region).getByRole("alert")).toHaveTextContent("Total 135.64 does not equal subtotal plus tax");
    expect(within(region).getByText("Consistency checks (1 passed, 1 failed)")).toBeInTheDocument();
    expect(screen.getByText("Extracted amounts or dates do not add up")).toBeInTheDocument();

    const vendor = fieldRow("Vendor name");
    expect(within(vendor).getByText("Kestrel Industrial Supply Inc.")).toBeInTheDocument();
    expect(within(vendor).getByText("printed: KESTREL INDUSTRIAL SUPPLY")).toBeInTheDocument();
    expect(within(fieldRow("Invoice number")).getByText(/Close match on page/)).toHaveTextContent("p. 1");
    expect(within(fieldRow("Invoice date")).getByText("ambiguous: 2026-03-04 or 2026-04-03")).toBeInTheDocument();
    const total = fieldRow("Total");
    expect(within(total).getByText("135.64 USD")).toBeInTheDocument();
    expect(within(total).getByText("AI model read: 0.00")).toBeInTheDocument();
    expect(within(total).getByText("40%")).toBeInTheDocument();
    expect(within(fieldRow("Due date")).getByText("not found")).toBeInTheDocument();

    expect(within(region).getByText("Line items")).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Hex bolt M10" })).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "71.55 USD" })).toBeInTheDocument();
  });

  it("highlights a value's source on the page preview", async () => {
    const user = signedIn();
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    mockApi({ "/api/v1/auth/me": () => jsonResponse(user), ...extractionRoutes() });
    const actor = userEvent.setup();
    renderApp(`/documents/${DOC_ID}`);

    await screen.findByRole("img", { name: "Page 1 preview" });
    expect(screen.queryByRole("img", { name: /^Source of/ })).not.toBeInTheDocument();
    await actor.click(within(fieldRow("Total")).getByRole("button", { name: "Show" }));

    const mark = await screen.findByRole("img", { name: "Source of Total" });
    const rect = mark.querySelector("rect");
    expect(rect).toHaveAttribute("x", "438");
    expect(rect).toHaveAttribute("width", "44");
    expect(scroll).toHaveBeenCalled();
    expect(within(fieldRow("Due date")).queryByRole("button", { name: "Show" })).not.toBeInTheDocument();
  });

  it("points at the field a comparison links to (?field=)", async () => {
    const user = signedIn();
    mockApi({ "/api/v1/auth/me": () => jsonResponse(user), ...extractionRoutes() });
    renderApp(`/documents/${DOC_ID}?field=fld-total`);

    expect(await screen.findByRole("img", { name: "Source of Total" })).toBeInTheDocument();
    expect(fieldRow("Total")).toHaveAttribute("aria-current", "true");
    expect(fieldRow("Invoice number")).not.toHaveAttribute("aria-current");
  });

  it("lets a reviewer correct a value and shows who corrected it", async () => {
    const user = signedIn(ANALYST_WITH_REVIEW);
    let current = extraction;
    const fetchMock = mockApi({
      "/api/v1/auth/me": () => jsonResponse(user),
      ...extractionRoutes(() => current),
      [`${EXTRACTION_URL}/fields/fld-invoice_number`]: () => {
        const corrected = field({
          field_path: "invoice_number",
          original_value: "INV-2026-0O42",
          evidence_status: "HUMAN",
          confidence: "1.0000",
          corrected_value: "INV-2026-0042",
          corrected_normalized: { value: "INV-2026-0042", status: "OK" },
          correction_note: "OCR read O for zero",
          corrected_by: { id: user.id, full_name: "Finance Analyst" },
          corrected_at: "2026-10-08T10:05:00Z",
        });
        current = { ...extraction, fields: extraction.fields.map((item) => (item.id === corrected.id ? corrected : item)) };
        return jsonResponse(corrected);
      },
    });
    const actor = userEvent.setup();
    renderApp(`/documents/${DOC_ID}`);

    await screen.findByRole("region", { name: "Extracted data" });
    await actor.click(within(fieldRow("Invoice number")).getByRole("button", { name: "Correct" }));
    const form = screen.getByRole("form", { name: "Correct Invoice number" });
    const input = within(form).getByLabelText("Value as printed (empty = not on the document)");
    expect(input).toHaveValue("INV-2026-0O42");
    await actor.clear(input);
    await actor.type(input, "INV-2026-0042");
    await actor.type(within(form).getByLabelText("Note (optional)"), "OCR read O for zero");
    await actor.click(within(form).getByRole("button", { name: "Save" }));

    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(patch?.[0]).toBe(`${EXTRACTION_URL}/fields/fld-invoice_number`);
    expect(JSON.parse(patch?.[1]?.body as string)).toEqual({ value: "INV-2026-0042", note: "OCR read O for zero" });
    expect(await screen.findByText("corrected by Finance Analyst · printed: INV-2026-0O42")).toBeInTheDocument();
    const row = fieldRow("Invoice number");
    expect(within(row).getByText("INV-2026-0042")).toBeInTheDocument();
    expect(within(row).getByText(/Entered by a reviewer/)).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Correct Invoice number" })).not.toBeInTheDocument();
  });

  it("hides corrections from users without review permission", async () => {
    const viewer = signedIn(["documents:read"]);
    mockApi({ "/api/v1/auth/me": () => jsonResponse(viewer), ...extractionRoutes() });
    renderApp(`/documents/${DOC_ID}`);
    const region = await screen.findByRole("region", { name: "Extracted data" });
    expect(within(region).queryByRole("button", { name: "Correct" })).not.toBeInTheDocument();
    expect(within(region).getAllByRole("button", { name: "Show" }).length).toBeGreaterThan(0);
  });

  it("explains when the document type has no extraction schema", async () => {
    const user = signedIn();
    mockApi({
      "/api/v1/auth/me": () => jsonResponse(user),
      ...extractionRoutes(),
      [EXTRACTION_URL]: () => problem(404, "No extraction for this document."),
    });
    renderApp(`/documents/${DOC_ID}`);
    const region = await screen.findByRole("region", { name: "Extracted data" });
    expect(within(region).getByText("No structured fields are extracted for this document type.")).toBeInTheDocument();
  });

  it("shows the matched vendor in the inbox", async () => {
    const user = signedIn();
    mockApi({
      "/api/v1/auth/me": () => jsonResponse(user),
      [LIST_URL]: () => jsonResponse(page([document({ vendor: KESTREL }), document({ id: "d2", vendor: null })])),
    });
    renderApp("/documents");
    const table = await screen.findByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Vendor" })).toBeInTheDocument();
    const rows = within(table).getAllByRole("row");
    expect(within(rows[1] as HTMLElement).getByText(KESTREL.canonical_name)).toBeInTheDocument();
  });
});
