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
  document_type: string | null;
  type_confidence: string | null;
  status: DocumentStatus;
  sensitivity: Sensitivity;
  source: string;
  owner: { id: string; full_name: string };
  department: Department | null;
  duplicate_of_id: string | null;
  duplicate_reason: string | null;
  processing_error: string | null;
  last_processed_at: string | null;
  created_at: string;
  updated_at: string;
  current_version: DocumentVersion | null;
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

export interface DocumentDetail extends DocumentSummary {
  inspection: Inspection | null;
  latest_job: ProcessingJob | null;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
