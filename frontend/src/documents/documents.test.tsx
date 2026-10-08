import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { saveSession } from "../auth/session";
import { filenameFromDisposition } from "../lib/api";
import type {
  Classification,
  DocumentDetail,
  DocumentSummary,
  DocumentTable,
  Page,
  PageDetail,
  PageSummary,
} from "../lib/types";
import { CURRENT_USER, jsonResponse, mockFetch, problem, renderApp } from "../test/utils";
import { documentsUrl } from "./format";

const LIST_URL = documentsUrl({ status: "", q: "", offset: 0 });
const DOC_ID = "5b0d8a3e-6b43-4c86-9f73-0b5b0f1f0a11";

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
  saveSession({ token: "header.payload.signature", expiresAt: Date.now() + 60_000 });
  return { ...CURRENT_USER, permissions };
}

describe("documents inbox", () => {
  it("lists documents with status, pages and duplicate flag", async () => {
    const user = signedIn();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(user),
      [LIST_URL]: () =>
        jsonResponse(
          page([
            document(),
            document({ id: "d2", display_filename: "scan.tiff", status: "PROCESSING", duplicate_of_id: DOC_ID }),
          ]),
        ),
    });
    renderApp("/documents");

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(within(rows[1] as HTMLElement).getByText("Processed")).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText("3.4 KB")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("Processing")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("duplicate")).toBeInTheDocument();
    expect(screen.getByText("1–2 of 2")).toBeInTheDocument();
  });

  it("uploads a file as multipart form data and refreshes the list", async () => {
    const user = signedIn();
    let listCalls = 0;
    const fetchMock = mockFetch({
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
    mockFetch({
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
    mockFetch({
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
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(user),
      [`/api/v1/documents/${DOC_ID}`]: () => jsonResponse(detail),
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
    const fetchMock = mockFetch({
      "/api/v1/auth/me": () => jsonResponse(user),
      [`/api/v1/documents/${DOC_ID}`]: () => jsonResponse(detail),
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
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(user),
      [`/api/v1/documents/${DOC_ID}`]: () => problem(404, "Document not found."),
    });
    renderApp(`/documents/${DOC_ID}`);
    expect(await screen.findByText("Document not found.")).toBeInTheDocument();
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
    };
  }

  it("shows type, evidence, review reasons, page text and preview, and tables", async () => {
    const user = signedIn();
    mockFetch({ "/api/v1/auth/me": () => jsonResponse(user), ...understandingRoutes() });
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
    const fetchMock = mockFetch({
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
    mockFetch({ "/api/v1/auth/me": () => jsonResponse(viewer), ...understandingRoutes() });
    renderApp(`/documents/${DOC_ID}`);
    await screen.findByRole("region", { name: "Document type" });
    expect(screen.queryByRole("button", { name: "Correct type" })).not.toBeInTheDocument();
  });

  it("shows the type in the inbox and filters by it", async () => {
    const user = signedIn();
    const typed = document({ document_type: "INVOICE", type_confidence: "0.9600" });
    const fetchMock = mockFetch({
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
