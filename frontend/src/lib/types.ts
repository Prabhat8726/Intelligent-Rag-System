/** Response shapes mirrored from the backend OpenAPI schema (docintel.api.schemas). */

export type Role = "ADMIN" | "MANAGER" | "ANALYST" | "REVIEWER" | "VIEWER";

export interface Department {
  id: string;
  name: string;
}

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  department: Department | null;
  is_active: boolean;
  last_login_at: string | null;
}

export interface CurrentUser extends User {
  permissions: string[];
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user: User;
}

export interface CheckResult {
  status: "ok" | "fail";
  detail: string | null;
  latency_ms: number | null;
}

export interface ReadinessResponse {
  status: "ready" | "not_ready";
  version: string;
  checks: Record<string, CheckResult>;
}

export type DocumentStatus = "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED" | "REVIEW_REQUIRED";
export type Sensitivity = "PUBLIC" | "INTERNAL" | "CONFIDENTIAL" | "RESTRICTED";
export type JobStatus = "QUEUED" | "PROCESSING" | "COMPLETED" | "FAILED" | "CANCELLED";
export type DocumentType =
  | "INVOICE"
  | "PURCHASE_ORDER"
  | "CONTRACT"
  | "RECEIPT"
  | "DELIVERY_NOTE"
  | "RESUME"
  | "BANK_STATEMENT"
  | "POLICY"
  | "OTHER";
export type ClassificationMethod = "LOCAL_MODEL" | "LLM" | "ENSEMBLE" | "HUMAN";
export type ExtractionMethod = "NATIVE" | "OCR";

export interface DocumentVersion {
  id: string;
  version_number: number;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  page_count: number;
  created_at: string;
}

export interface DocumentSummary {
  id: string;
  display_filename: string;
  document_type: DocumentType | null;
  type_confidence: string | null;
  status: DocumentStatus;
  sensitivity: Sensitivity;
  source: string;
  owner: { id: string; full_name: string };
  department: Department | null;
  duplicate_of_id: string | null;
  duplicate_reason: string | null;
  processing_error: string | null;
  review_reasons: string[];
  last_processed_at: string | null;
  created_at: string;
  updated_at: string;
  current_version: DocumentVersion | null;
  vendor?: VendorSummary | null;
}

export interface VendorSummary {
  id: string;
  canonical_name: string;
}

export interface ProcessingJob {
  id: string;
  job_type: string;
  status: JobStatus;
  attempts: number;
  max_attempts: number;
  stage: string | null;
  stage_timings: Record<string, number>;
  last_error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
}

export interface PageInspection {
  page_number: number;
  width: number;
  height: number;
  unit: "pt" | "px";
  rotation: number;
  text_chars: number;
  image_objects: number;
  method: "NATIVE" | "OCR";
  dpi: [number, number] | null;
}

export interface Inspection {
  kind: "native_pdf" | "scanned_pdf" | "mixed_pdf" | "image";
  page_count: number;
  pages: PageInspection[];
  pages_needing_ocr: number[];
  version: number;
}

export interface ClassificationSignals {
  local?: { label: DocumentType; probability: number }[];
  keywords?: Partial<Record<DocumentType, string[]>>;
  threshold?: number;
  external_ai?: { allowed: boolean; effective_sensitivity: Sensitivity; reason: string };
  llm?: { used: boolean; reason?: string; label?: DocumentType; quote_found?: boolean; model?: string };
  previous?: { label: DocumentType; method: ClassificationMethod; confidence: string } | null;
  reason?: string;
}

export interface Classification {
  id: string;
  label: DocumentType;
  confidence: string;
  method: ClassificationMethod;
  model_version: string | null;
  signals: ClassificationSignals;
  note: string | null;
  created_by: { id: string; full_name: string } | null;
  is_current: boolean;
  created_at: string;
}

export interface PageSummary {
  page_number: number;
  width: number;
  height: number;
  unit: "pt" | "px";
  rotation_applied: number;
  extraction_method: ExtractionMethod;
  ocr_confidence: string | null;
  word_count: number;
  preview_width: number | null;
  preview_height: number | null;
  has_preview: boolean;
}

/** [text, x0, y0, x1, y1, ocr confidence | null, size] in page units, top-left origin. */
export type PageWord = [string, number, number, number, number, number | null, number];

export interface PageDetail extends PageSummary {
  text: string;
  words: PageWord[];
  layout: { lines: unknown[]; blocks: { kind: string; bbox: number[] }[]; warnings: string[] };
}

export interface TableRow {
  row_index: number;
  page_number: number;
  cells: string[];
  bbox: number[];
}

export interface DocumentTable {
  id: string;
  table_index: number;
  page_start: number;
  page_end: number;
  header: string[];
  bbox: number[];
  extraction_method: "NATIVE" | "OCR" | "MIXED";
  confidence: string;
  row_count: number;
  rows: TableRow[];
}

export interface SensitivityAssessment {
  findings: { kind: string; count: number; pages: number[]; level: Sensitivity | null }[];
  type_minimum: Sensitivity | null;
  detected: Sensitivity | null;
}

export interface DocumentDetail extends DocumentSummary {
  inspection: Inspection | null;
  sensitivity_assessment: SensitivityAssessment | null;
  latest_job: ProcessingJob | null;
  classification: Classification | null;
  classification_history: Classification[];
  pages: PageSummary[];
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type EvidenceStatus = "VERIFIED" | "FUZZY" | "UNSUPPORTED" | "NOT_FOUND" | "HUMAN";
export type FieldOrigin = "LOCAL" | "LLM" | "BOTH" | "DERIVED" | "HUMAN";
export type ReviewLevel = "AUTO" | "ANALYST_REVIEW" | "MANDATORY_REVIEW";

export interface NormalizedValue {
  value: string | number | boolean | null;
  status: "OK" | "UNCERTAIN" | "INVALID" | "CONFIRMED_EMPTY";
  rule?: string;
  alternatives?: string[];
  currency?: string | null;
  currency_from?: string;
  vendor?: { vendor_id: string; canonical_name: string; score: number; method: string } | null;
  [key: string]: unknown;
}

export interface ExtractedField {
  id: string;
  field_path: string;
  field_name: string;
  group_name: string | null;
  row_index: number | null;
  value_type: string;
  is_required: boolean;
  original_value: string | null;
  normalized_value: NormalizedValue | null;
  page_number: number | null;
  source_text: string | null;
  bbox: number[] | null;
  evidence_status: EvidenceStatus;
  origin: FieldOrigin | null;
  method: string | null;
  confidence: string;
  confidence_signals: Record<string, unknown>;
  alternatives: { origin: string; value: string; page: number | null; evidence: string }[];
  corrected_value: string | null;
  corrected_normalized: NormalizedValue | null;
  correction_note: string | null;
  corrected_by: { id: string; full_name: string } | null;
  corrected_at: string | null;
}

export interface ExtractionCheck {
  code: string;
  status: "PASS" | "FAIL";
  fields: string[];
  expected: string;
  actual: string;
  message: string;
}

export interface ExtractionSignals {
  llm?: { mode: string; used: boolean; reason?: string; model?: string; cache_hit?: boolean; errors?: string[] };
  external_ai?: { allowed: boolean; effective_sensitivity: Sensitivity; reason: string };
  context?: { date_order: string | null; date_order_reason: string | null; currency: string | null };
  [key: string]: unknown;
}

export interface Extraction {
  id: string;
  document_version_id: string;
  schema_name: string;
  schema_version: number;
  status: "SUCCEEDED" | "PARTIAL" | "FAILED";
  method: "LOCAL" | "LLM" | "COMBINED";
  provider: string | null;
  model: string | null;
  prompt_version: string | null;
  overall_confidence: string;
  review_level: ReviewLevel;
  checks: ExtractionCheck[];
  signals: ExtractionSignals;
  validation_error_count: number;
  created_at: string;
  fields: ExtractedField[];
  vendor: VendorSummary | null;
}

/** Where to draw attention on a page preview (an extracted value's source). */
export interface Highlight {
  page: number;
  bbox: number[];
  label: string;
}
