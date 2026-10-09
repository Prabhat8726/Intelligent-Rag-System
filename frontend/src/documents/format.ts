import type {
  ClassificationMethod,
  DocumentStatus,
  DocumentType,
  EvidenceStatus,
  ExtractedField,
  ReviewLevel,
} from "../lib/types";

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
  EXTRACTION_FAILED: "No fields could be extracted",
  MISSING_REQUIRED_FIELDS: "Required fields are missing",
  EXTRACTION_UNCERTAIN: "Some extracted values are uncertain",
  EXTRACTION_INCONSISTENT: "Extracted amounts or dates do not add up",
};

export const REVIEW_LEVEL_LABELS: Record<ReviewLevel, string> = {
  AUTO: "Auto-accepted",
  ANALYST_REVIEW: "Analyst review",
  MANDATORY_REVIEW: "Mandatory review",
};

export const REVIEW_LEVEL_STYLES: Record<ReviewLevel, string> = {
  AUTO: "bg-emerald-50 text-emerald-700",
  ANALYST_REVIEW: "bg-amber-50 text-amber-800",
  MANDATORY_REVIEW: "bg-red-50 text-red-700",
};

export const EVIDENCE_LABELS: Record<EvidenceStatus, string> = {
  VERIFIED: "Verified on page",
  FUZZY: "Close match on page",
  UNSUPPORTED: "Quote does not contain the value",
  NOT_FOUND: "Not found on the document",
  HUMAN: "Entered by a reviewer",
};

export const EXTRACTION_METHOD_LABELS: Record<string, string> = {
  LOCAL: "Layout rules",
  LLM: "AI model",
  COMBINED: "Layout rules + AI model",
};

/** "purchase_order_number" -> "Purchase order number". */
export function fieldLabel(name: string): string {
  const words = name.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Display value: the reviewer's correction, else the normalized value, else the printed text. */
export function fieldValue(field: ExtractedField): string {
  if (field.corrected_value !== null) return field.corrected_value === "" ? "(not on document)" : field.corrected_value;
  const normalized = field.normalized_value;
  if (normalized?.vendor) return normalized.vendor.canonical_name;
  if (normalized && normalized.value !== null) {
    if (field.value_type === "MONEY" && normalized.currency) return `${String(normalized.value)} ${normalized.currency}`;
    if (field.value_type === "PERCENT") return `${String(Math.round(Number(normalized.value) * 10000) / 100)}%`;
    if (field.value_type === "DAYS") return `${String(normalized.value)} days`;
    return String(normalized.value);
  }
  return field.original_value ?? "—";
}

export function formatPercent(value: string | number | null): string {
  if (value === null) return "—";
  return `${Math.round(Number(value) * 100)}%`;
}

export const PAGE_SIZE = 25;

export function documentsUrl(params: {
  status: string;
  type?: string;
  vendor?: string;
  q: string;
  offset: number;
}): string {
  const search = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(params.offset) });
  if (params.status) search.set("status", params.status);
  if (params.type) search.set("document_type", params.type);
  if (params.vendor) search.set("vendor_id", params.vendor);
  if (params.q.trim()) search.set("q", params.q.trim());
  return `/api/v1/documents?${search.toString()}`;
}
