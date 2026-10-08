import type { DocumentStatus } from "../lib/types";

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

export const PAGE_SIZE = 25;

export function documentsUrl(params: { status: string; q: string; offset: number }): string {
  const search = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(params.offset) });
  if (params.status) search.set("status", params.status);
  if (params.q.trim()) search.set("q", params.q.trim());
  return `/api/v1/documents?${search.toString()}`;
}
