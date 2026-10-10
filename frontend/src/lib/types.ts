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
  /** The open review task: present exactly while the status is REVIEW_REQUIRED. */
  review?: ReviewTaskBrief | null;
  /** The current extraction's confidence and routing (lists only). */
  extraction?: { overall_confidence: string; review_level: ReviewLevel } | null;
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

// ------------------------------------------------------------------ Phase 5: matching & review
export type ItemStatus = "MATCH" | "MISMATCH" | "MISSING" | "UNCERTAIN";
export type ComparisonRole = "INVOICE" | "PURCHASE_ORDER" | "DELIVERY_NOTE";
export type ComparisonType = "INVOICE_PO" | "INVOICE_DELIVERY" | "INVOICE_PO_DELIVERY" | "PO_DELIVERY";
export type RuleOutcome = "PASS" | "FAIL" | "WARN" | "ERROR" | "NOT_APPLICABLE";
export type Severity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type ReviewPriority = "URGENT" | "HIGH" | "NORMAL" | "LOW";
export type ReviewTaskType =
  | "DUPLICATE_REVIEW"
  | "DISCREPANCY_REVIEW"
  | "EXTRACTION_REVIEW"
  | "CLASSIFICATION_REVIEW"
  | "REQUESTED_REVIEW";
export type ReviewTaskStatus = "OPEN" | "IN_PROGRESS" | "RESOLVED" | "CANCELLED";
export type ReviewResolution = "APPROVED" | "CORRECTED" | "REJECTED" | "CLEARED";

export interface UserRef {
  id: string;
  full_name: string;
}

export interface ComparisonSide {
  role: ComparisonRole;
  document_id: string | null;
  document_label: string;
  value: string | null;
  printed: string | null;
  field_id: string | null;
  page: number | null;
  source_text: string | null;
  bbox: number[] | null;
  confidence: number | null;
  corrected: boolean;
}

export interface ComparisonItem {
  id: string;
  position: number;
  item_key: string;
  category: "HEADER" | "LINE_ITEM";
  check_name: string;
  line_key: string | null;
  status: ItemStatus;
  left_value: string | null;
  right_value: string | null;
  difference: Record<string, string> | null;
  tolerance: Record<string, string> | null;
  explanation: string;
  left: ComparisonSide[];
  right: ComparisonSide[];
}

export interface ComparisonDocument {
  document_id: string;
  role: ComparisonRole;
  position: number;
  document_version_id: string | null;
  extraction_id: string | null;
  display_filename: string;
  document_type: DocumentType | null;
}

export interface ComparisonSummary {
  id: string;
  comparison_type: ComparisonType;
  origin: "AUTO" | "MANUAL";
  subject_document_id: string;
  summary: Record<ItemStatus, number>;
  created_at: string;
  requested_by: UserRef | null;
  documents: ComparisonDocument[];
}

export interface Comparison extends ComparisonSummary {
  settings: Record<string, unknown>;
  items: ComparisonItem[];
}

export interface Rule {
  id: string;
  code: string;
  rule_type: string;
  name: string;
  description: string;
  applies_to: DocumentType[];
  params: Record<string, unknown>;
  severity: Severity;
  is_enabled: boolean;
  version: number;
  updated_by: UserRef | null;
  updated_at: string;
  params_schema: Record<string, unknown>;
}

export interface RuleResult {
  id: string;
  rule_code: string;
  rule_version: number;
  outcome: RuleOutcome;
  severity: Severity;
  message: string;
  evidence: Record<string, unknown>;
  items: string[];
  comparison_id: string | null;
  evaluated_at: string;
}

export interface ReviewReason {
  key: string;
  category: string;
  code: string;
  severity: Severity;
  message: string;
}

export interface ReviewTaskBrief {
  id: string;
  task_type: ReviewTaskType;
  status: ReviewTaskStatus;
  priority: ReviewPriority;
  due_at: string | null;
  assigned_to: UserRef | null;
}

export interface ReviewTask extends ReviewTaskBrief {
  document_id: string;
  document_version_id: string | null;
  reasons: ReviewReason[];
  claimed_at: string | null;
  resolution: ReviewResolution | null;
  resolution_note: string | null;
  resolved_by: UserRef | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
  overdue: boolean;
}

export interface ReviewTaskListItem extends ReviewTask {
  document: { id: string; display_filename: string; document_type: DocumentType | null; status: DocumentStatus };
}

export interface Duplicate {
  kind: string;
  document_id: string;
  display_filename: string;
  direction: "original" | "copy";
  evidence: Record<string, unknown>;
}

export interface Findings {
  comparisons: ComparisonSummary[];
  rule_results: RuleResult[];
  duplicates: Duplicate[];
  open_task: ReviewTask | null;
  review_history: ReviewTask[];
}

export interface VersionInfo extends DocumentVersion {
  processed: boolean;
  is_current: boolean;
}

export interface Clause {
  key: string;
  number: string | null;
  title: string;
  text: string;
  page: number | null;
}

export interface ClauseDiff {
  change: "UNCHANGED" | "MODIFIED" | "ADDED" | "REMOVED";
  title: string;
  old: Clause | null;
  new: Clause | null;
  similarity: number;
  renumbered: boolean;
  operations: { op: "equal" | "insert" | "delete" | "replace"; old: string; new: string }[];
}

export interface VersionComparison {
  document_id: string;
  from_version: number;
  to_version: number;
  summary: Record<ClauseDiff["change"], number>;
  clauses: ClauseDiff[];
}

// ---------------------------------------------------------------- knowledge base (Module 12, 13)
export type KnowledgeCategory =
  | "POLICY"
  | "PROCEDURE"
  | "CONTRACT_GUIDELINE"
  | "FAQ"
  | "COMPLIANCE"
  | "PUBLIC_REFERENCE";

export type KnowledgeStatus = "PROCESSING" | "ACTIVE" | "SUPERSEDED" | "ARCHIVED" | "FAILED";

export interface KnowledgeDocument {
  id: string;
  document_key: string;
  title: string;
  category: KnowledgeCategory;
  version_label: string | null;
  department: Department | null;
  sensitivity: Sensitivity;
  effective_sensitivity: Sensitivity | null;
  effective_from: string | null;
  effective_to: string | null;
  status: KnowledgeStatus;
  supersedes_id: string | null;
  source_format: string;
  original_filename: string;
  size_bytes: number;
  page_count: number | null;
  chunk_count: number;
  embedding_model: string | null;
  embedding_note: string | null;
  processing_error: string | null;
  processed_at: string | null;
  uploaded_by: { id: string; full_name: string };
  created_at: string;
  updated_at: string;
}

export interface KnowledgeDocumentDetail extends KnowledgeDocument {
  sha256: string;
  mime_type: string;
  latest_job: ProcessingJob | null;
}

export interface KnowledgeChunk {
  id: string;
  chunk_index: number;
  section_path: string;
  heading: string;
  kind: string;
  content: string;
  page_start: number | null;
  page_end: number | null;
  token_count: number;
  embedding_model: string | null;
  has_embedding: boolean;
  effective_from: string | null;
  effective_to: string | null;
}

export interface Evidence {
  sufficient: boolean;
  term_coverage: number;
  dense_similarity: number | null;
  reason: string;
}

export interface RetrievalInfo {
  mode: "hybrid" | "full_text" | "dense" | "none";
  embedding_model: string | null;
  as_of: string;
  query_terms: string[];
  timings_ms: Record<string, number>;
}

export interface Passage {
  chunk_id: string;
  knowledge_document_id: string;
  document_key: string;
  title: string;
  version_label: string | null;
  category: KnowledgeCategory;
  status: KnowledgeStatus;
  section_path: string;
  heading: string;
  content: string;
  page_start: number | null;
  page_end: number | null;
  effective_from: string | null;
  effective_to: string | null;
  score: number;
  dense_similarity: number | null;
  text_score: number | null;
  term_coverage: number;
}

export interface KnowledgeSearchResponse {
  query: string;
  passages: Passage[];
  evidence: Evidence;
  retrieval: RetrievalInfo;
}

export type AnswerStatus = "ANSWERED" | "PARTIALLY_SUPPORTED" | "INSUFFICIENT_EVIDENCE" | "RETRIEVAL_ONLY";

export interface AnswerSource {
  label: string;
  cited: boolean;
  sent_to_model: boolean;
  knowledge_document_id: string;
  document_key: string;
  title: string;
  version_label: string | null;
  status: KnowledgeStatus;
  section_path: string;
  page_start: number | null;
  page_end: number | null;
  effective_from: string | null;
  effective_to: string | null;
  chunk_ids: string[];
  content: string;
}

export interface AnswerClaim {
  text: string;
  citations: string[];
  grounded: boolean;
  grounding: number;
}

export interface KnowledgeAnswer {
  question: string;
  status: AnswerStatus;
  answer: string | null;
  claims: AnswerClaim[];
  sources: AnswerSource[];
  evidence: Evidence;
  retrieval: RetrievalInfo;
  notices: string[];
  model: string | null;
  provider: string | null;
}

// ---------------------------------------------------------------- document search (Module 28)
export interface SearchComparison {
  op: "gt" | "gte" | "lt" | "lte" | "eq";
  value: string;
}

export interface SearchInterpretation {
  document_types: DocumentType[];
  vendor: string | null;
  vendors_matched: string[];
  payment_terms_days: SearchComparison | null;
  total: SearchComparison | null;
  date_from: string | null;
  date_to: string | null;
  text: string;
  recognized: string[];
}

export interface SearchHit {
  document: DocumentSummary;
  vendor_name: string | null;
  document_date: string | null;
  total: string | null;
  payment_terms_days: number | null;
  score: number | null;
  reasons: string[];
  snippet: { chunk_id: string; text: string; page_start: number | null; page_end: number | null } | null;
}

export interface DocumentSearchResponse {
  query: string;
  mode: "structured" | "text" | "structured+text";
  interpretation: SearchInterpretation;
  total: number;
  results: SearchHit[];
}

// ---------------------------------------------------------------- agent analysis (Phase 7)
export type AnalysisStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
export type AnalysisIntent =
  | "VERIFY_DOCUMENT"
  | "INVESTIGATE_DISCREPANCY"
  | "CHECK_DUPLICATE"
  | "COMPARE_DOCUMENTS"
  | "POLICY_QUESTION"
  | "FIND_DOCUMENTS";
export type FindingCategory =
  | "OBSERVED_FACT"
  | "RULE_RESULT"
  | "RETRIEVED_KNOWLEDGE"
  | "AI_INFERENCE"
  | "UNCERTAINTY";
export type ActionType =
  | "APPROVE_FOR_PAYMENT"
  | "HOLD_FOR_REVIEW"
  | "REQUEST_VENDOR_CLARIFICATION"
  | "REJECT_DUPLICATE"
  | "NO_ACTION";
export type ConfidenceLevel = "HIGH" | "MEDIUM" | "LOW";

export interface AnalysisFinding {
  category: FindingCategory;
  statement: string;
  evidence: string[];
  source: "rules" | "model";
  grounded: boolean;
}

export interface AnalysisEvidence {
  label: string;
  kind: "DOCUMENT" | "FIELD" | "RULE" | "COMPARISON" | "KNOWLEDGE";
  document_id: string | null;
  ref: string;
  text: string;
}

export interface AnalysisDocument {
  label: string | null;
  role: "subject" | "related";
  document_id: string;
  filename: string;
  document_type: DocumentType | null;
  status: string;
  vendor_name: string | null;
  document_date: string | null;
  total: string | null;
  currency: string | null;
  effective_sensitivity: string | null;
}

export interface AnalysisSource {
  label: string | null;
  sent_to_model: boolean;
  chunk_id: string;
  knowledge_document_id: string;
  title: string;
  version_label: string | null;
  section_path: string;
  page_start: number | null;
  page_end: number | null;
  effective_from: string | null;
  effective_to: string | null;
  content: string;
  query: string | null;
}

export interface Recommendation {
  action: ActionType;
  target_document_id: string | null;
  rationale: string;
  evidence: string[];
  risk: "NONE" | "LOW" | "MEDIUM" | "HIGH";
  requires_approval: boolean;
  required_role: string | null;
  source: "rules" | "model";
  guardrail_notes: string[];
}

export interface ActionRecord {
  action: ActionType;
  status: "EXECUTED" | "PROPOSED" | "SKIPPED" | "FAILED";
  detail: string;
  tool_call_id: string | null;
  review_task_id: string | null;
  required_role: string | null;
}

export interface AnalysisResult {
  summary: string;
  summary_source: "rules" | "model";
  intent: AnalysisIntent;
  documents: AnalysisDocument[];
  findings: AnalysisFinding[];
  evidence: AnalysisEvidence[];
  sources: AnalysisSource[];
  comparisons: { comparison_id: string; comparison_type: ComparisonType; summary: Record<string, number> }[];
  confidence: { level: ConfidenceLevel; score: number; factors: { factor: string; effect: number; detail: string }[] };
  recommendation: Recommendation;
  action: ActionRecord | null;
  notices: string[];
  model: { provider: string; model: string } | null;
}

export interface ToolCallRecord {
  id: string;
  node_name: string | null;
  tool_name: string;
  status: "SUCCEEDED" | "FAILED" | "DENIED" | "INVALID";
  error: string | null;
  latency_ms: string;
  arguments: Record<string, unknown>;
  result_summary: Record<string, unknown> | null;
  created_at: string;
}

export interface AnalysisSummary {
  id: string;
  status: AnalysisStatus;
  query: string;
  document_ids: string[];
  intent: AnalysisIntent | null;
  recommendation: ActionType | null;
  confidence: ConfidenceLevel | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface AnalysisRun extends AnalysisSummary {
  graph_version: string;
  allow_safe_actions: boolean;
  plan: {
    intent: AnalysisIntent;
    document_query: string | null;
    identifiers: string[];
    knowledge_questions: string[];
    focus_fields: string[];
    source: "rules" | "model";
  } | null;
  result: AnalysisResult | null;
  trace: { node: string; duration_ms: number; tool_calls: number }[];
  tool_call_log: ToolCallRecord[];
  usage: {
    tool_calls: number;
    llm_calls: number;
    input_tokens: number | null;
    output_tokens: number | null;
    estimated_cost_usd: string | null;
  };
}

export interface AnalysisPage {
  items: AnalysisSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ApiToken {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface ApiTokenCreated extends ApiToken {
  token: string;
}

// ---------------------------------------------------------------- workflows (Phase 8)
export type WorkflowType = "INVOICE_PROCESSING" | "CONTRACT_REVIEW";
export type WorkflowStatus =
  | "QUEUED"
  | "RUNNING"
  | "AWAITING_APPROVAL"
  | "COMPLETED"
  | "REJECTED"
  | "FAILED"
  | "CANCELLED";
export type StepStatus = "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "SKIPPED";
export type WorkflowActionType =
  | "APPROVE_FOR_PAYMENT"
  | "REJECT_DUPLICATE"
  | "REQUEST_VENDOR_CLARIFICATION"
  | "HOLD_FOR_REVIEW"
  | "APPROVE_CONTRACT"
  | "REQUEST_LEGAL_REVIEW";
export type ActionStatus = "PROPOSED" | "AWAITING_APPROVAL" | "APPROVED" | "REJECTED" | "EXECUTED" | "FAILED";
export type ActionRisk = "LOW" | "MEDIUM" | "HIGH";

export interface PersonRef {
  id: string;
  email: string;
  full_name: string;
}

export interface WorkflowDocument {
  id: string;
  filename: string;
  document_type: DocumentType | null;
  status: DocumentStatus;
  version_number: number | null;
  is_current_version: boolean;
}

export interface WorkflowStep {
  sequence: number;
  step_name: string;
  status: StepStatus;
  output: Record<string, unknown>;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface ActionTransition {
  from_status: ActionStatus | null;
  to_status: ActionStatus;
  actor_type: "USER" | "SYSTEM" | "AGENT" | "WORKER" | "ANONYMOUS";
  actor: PersonRef | null;
  reason: string | null;
  created_at: string;
}

export interface WorkflowAction {
  id: string;
  action_type: WorkflowActionType;
  title: string;
  status: ActionStatus;
  risk_level: ActionRisk;
  requires_approval: boolean;
  required_role: string | null;
  proposed_by_type: "RULES" | "AGENT" | "USER";
  rationale: string;
  confidence_level: string | null;
  confidence_score: number | null;
  payload: Record<string, unknown>;
  decided_by: PersonRef | null;
  decided_at: string | null;
  decision_reason: string | null;
  executed_at: string | null;
  execution_result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  transitions: ActionTransition[];
  can_decide: boolean;
  blockers: string[];
}

export interface PendingAction {
  id: string;
  action_type: WorkflowActionType;
  title: string;
  risk_level: ActionRisk;
  required_role: string | null;
}

export interface WorkflowSummary {
  id: string;
  workflow_type: WorkflowType;
  title: string;
  status: WorkflowStatus;
  outcome: string | null;
  trigger: "MANUAL" | "AUTO";
  document: WorkflowDocument;
  initiated_by: PersonRef;
  current_step: string | null;
  pending_action: PendingAction | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Workflow extends WorkflowSummary {
  definition_version: number;
  steps: WorkflowStep[];
  actions: WorkflowAction[];
  agent_run_id: string | null;
  analysis: AnalysisResult | null;
  report_ids: string[];
}

export interface WorkflowPage {
  items: WorkflowSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface WorkflowCounts {
  awaiting_my_decision: number;
}

// ---------------------------------------------------------------- reports (Phase 8)
export type ReportType =
  | "INVOICE_VERIFICATION"
  | "CONTRACT_REVIEW"
  | "DOCUMENT_COMPARISON"
  | "COMPLIANCE_REVIEW"
  | "AI_ANALYSIS";

export interface ReportSummary {
  id: string;
  report_type: ReportType;
  subject_type: "DOCUMENT" | "COMPARISON" | "AGENT_RUN";
  subject_id: string;
  title: string;
  document_ids: string[];
  workflow_id: string | null;
  template_version: number;
  content_sha256: string;
  as_of: string;
  generated_by_email: string;
  created_at: string;
}

export interface Report extends ReportSummary {
  content: string;
  snapshot: Record<string, unknown>;
}

export interface ReportPage {
  items: ReportSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ReportVerification {
  report_id: string;
  matches: boolean;
  content_sha256: string;
}

// ---------------------------------------------------------------- administration (Phase 8)
export interface AdminUser extends User {
  failed_login_attempts: number;
  locked_until: string | null;
  created_at: string;
}

export interface UserPage {
  items: AdminUser[];
  total: number;
  limit: number;
  offset: number;
}

export interface AuditEvent {
  id: number;
  occurred_at: string;
  actor: PersonRef | null;
  actor_type: string;
  actor_role: string | null;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  outcome: "SUCCESS" | "FAILURE" | "DENIED";
  request_id: string | null;
  ip_address: string | null;
  user_agent: string | null;
  details: Record<string, unknown>;
}

export interface AuditEventPage {
  items: AuditEvent[];
  next_before_id: number | null;
}

// ------------------------------------------------------------------------------ dashboard
export interface DashboardSummary {
  days: number;
  since: string;
  until: string;
  documents: {
    total: number;
    uploaded_in_period: number;
    by_status: Record<string, number>;
    by_type: Record<string, number>;
  };
  processing: {
    processed_in_period: number;
    average_seconds: number | null;
    p95_seconds: number | null;
    failed_in_period: number;
  };
  review_queue: {
    open: number;
    overdue: number;
    by_priority: Record<string, number>;
    by_type: Record<string, number>;
  };
  discrepancies: {
    documents_failing: number;
    by_rule: { rule_code: string; documents: number }[];
  };
  investigations: {
    scope: "all" | "mine";
    in_period: number;
    by_status: Record<string, number>;
    by_recommendation: Record<string, number>;
  };
  workflows: {
    awaiting_approval: number;
    finished_in_period: number;
    by_outcome: Record<string, number>;
    by_status: Record<string, number>;
  };
  confidence: { day: string; processed: number; extraction_confidence: number | null; auto_accepted: number }[];
  activity: {
    id: number;
    occurred_at: string;
    action: string;
    outcome: string;
    actor: string;
    document_id: string | null;
    document_name: string | null;
    workflow_id: string | null;
  }[];
}

// ------------------------------------------------------------------ evaluation (Phase 10)
export interface EvaluationHeadline {
  label: string;
  value: string | null;
}

export interface EvaluationGateSummary {
  passed: boolean;
  mode: string;
  checks: number;
  failed: number;
}

export interface EvaluationSummary {
  id: string;
  suite: string;
  title: string;
  quick: boolean;
  git_revision: string | null;
  run_at: string;
  recorded_at: string;
  source: "RUN" | "IMPORT";
  recorded_by: string | null;
  gates: EvaluationGateSummary | null;
  headlines: EvaluationHeadline[];
}

export interface EvaluationPage {
  items: EvaluationSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface EvaluationGateCheck {
  metric: string[];
  value: number | null;
  min: number | null;
  max: number | null;
  passed: boolean;
  problem: string | null;
  why: string;
}

export interface EvaluationReportTable {
  heading: string;
  header: string[];
  rows: string[][];
}

export interface EvaluationDetail extends EvaluationSummary {
  dataset: Record<string, unknown>;
  config: Record<string, unknown>;
  environment: Record<string, unknown>;
  metrics: Record<string, unknown>;
  notes: string[];
  tables: EvaluationReportTable[];
  gate_checks: EvaluationGateCheck[];
  report_markdown: string;
}
