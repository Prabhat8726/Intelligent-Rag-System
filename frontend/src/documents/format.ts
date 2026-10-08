import type { ClassificationMethod, DocumentStatus, DocumentType } from "../lib/types";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDateTime(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return "—";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export const ACTIVE_STATUSES: ReadonlySet<DocumentStatus> = new Set(["PENDING", "PROCESSING"]);

const STATUS_STYLES: Record<DocumentStatus, string> = {
  PENDING: "bg-slate-100 text-slate-700",
  PROCESSING: "bg-blue-50 text-blue-800",
  COMPLETED: "bg-emerald-50 text-emerald-700",
  FAILED: "bg-red-50 text-red-700",
  REVIEW_REQUIRED: "bg-amber-50 text-amber-800",
};

const STATUS_LABELS: Record<DocumentStatus, string> = {
  PENDING: "Queued",
  PROCESSING: "Processing",
  COMPLETED: "Processed",
  FAILED: "Failed",
  REVIEW_REQUIRED: "Needs review",
};

export function statusStyle(status: DocumentStatus): string {
  return STATUS_STYLES[status];
}

export function statusLabel(status: DocumentStatus): string {
  return STATUS_LABELS[status];
}

export const INSPECTION_LABELS: Record<string, string> = {
  native_pdf: "Digital PDF (text layer)",
  scanned_pdf: "Scanned PDF (needs OCR)",
  mixed_pdf: "Mixed PDF (some pages need OCR)",
  image: "Image (needs OCR)",
};

export const DOCUMENT_TYPE_LABELS: Record<DocumentType, string> = {
  INVOICE: "Invoice",
  PURCHASE_ORDER: "Purchase order",
  CONTRACT: "Contract",
  RECEIPT: "Receipt",
  DELIVERY_NOTE: "Delivery note",
  RESUME: "Resume",
  BANK_STATEMENT: "Bank statement",
  POLICY: "Policy",
  OTHER: "Other",
};

export const DOCUMENT_TYPES = Object.keys(DOCUMENT_TYPE_LABELS) as DocumentType[];

export const METHOD_LABELS: Record<ClassificationMethod, string> = {
  LOCAL_MODEL: "Local model",
  LLM: "AI model",
  ENSEMBLE: "Local model confirmed by AI",
  HUMAN: "Human",
};

export const REVIEW_REASON_LABELS: Record<string, string> = {
  CLASSIFICATION_UNCERTAIN: "Document type is uncertain",
  NO_TEXT_FOUND: "No readable text was found",
  LOW_OCR_CONFIDENCE: "Text recognition confidence is low on at least one page",
  OCR_FAILED: "Text recognition failed on at least one page",
};

export function formatPercent(value: string | number | null): string {
  if (value === null) return "—";
  return `${Math.round(Number(value) * 100)}%`;
}

export const PAGE_SIZE = 25;

export function documentsUrl(params: { status: string; type?: string; q: string; offset: number }): string {
  const search = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(params.offset) });
  if (params.status) search.set("status", params.status);
  if (params.type) search.set("document_type", params.type);
  if (params.q.trim()) search.set("q", params.q.trim());
  return `/api/v1/documents?${search.toString()}`;
}
