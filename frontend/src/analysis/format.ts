import type { ActionType, AnalysisIntent, AnalysisStatus, ConfidenceLevel, FindingCategory } from "../lib/types";

export const INTENT_LABELS: Record<AnalysisIntent, string> = {
  VERIFY_DOCUMENT: "Verify a document",
  INVESTIGATE_DISCREPANCY: "Explain a discrepancy",
  CHECK_DUPLICATE: "Check for duplicates",
  COMPARE_DOCUMENTS: "Compare documents",
  POLICY_QUESTION: "Policy question",
  FIND_DOCUMENTS: "Find documents",
};

export const STATUS_STYLES: Record<AnalysisStatus, string> = {
  QUEUED: "bg-slate-100 text-slate-700",
  RUNNING: "bg-blue-50 text-blue-800",
  COMPLETED: "bg-emerald-50 text-emerald-700",
  FAILED: "bg-red-50 text-red-700",
};

export const CATEGORY_LABELS: Record<FindingCategory, string> = {
  OBSERVED_FACT: "Observed fact",
  RULE_RESULT: "Rule result",
  RETRIEVED_KNOWLEDGE: "Policy",
  AI_INFERENCE: "AI inference",
  UNCERTAINTY: "Uncertainty",
};

export const CATEGORY_STYLES: Record<FindingCategory, string> = {
  OBSERVED_FACT: "bg-slate-100 text-slate-700",
  RULE_RESULT: "bg-blue-50 text-blue-900",
  RETRIEVED_KNOWLEDGE: "bg-violet-50 text-violet-800",
  AI_INFERENCE: "bg-amber-50 text-amber-800",
  UNCERTAINTY: "bg-orange-50 text-orange-800",
};

/** Display order: facts and rule outcomes before interpretation. */
export const CATEGORY_ORDER: FindingCategory[] = [
  "RULE_RESULT",
  "OBSERVED_FACT",
  "RETRIEVED_KNOWLEDGE",
  "AI_INFERENCE",
  "UNCERTAINTY",
];

export const ACTION_LABELS: Record<ActionType, string> = {
  APPROVE_FOR_PAYMENT: "Approve for payment",
  HOLD_FOR_REVIEW: "Hold for review",
  REQUEST_VENDOR_CLARIFICATION: "Ask the vendor to clarify",
  REJECT_DUPLICATE: "Reject as duplicate",
  NO_ACTION: "No action",
};

export const CONFIDENCE_STYLES: Record<ConfidenceLevel, string> = {
  HIGH: "bg-emerald-50 text-emerald-700",
  MEDIUM: "bg-amber-50 text-amber-800",
  LOW: "bg-red-50 text-red-700",
};

export const NODE_LABELS: Record<string, string> = {
  understand_request: "Understand the request",
  identify_documents: "Identify documents",
  inspect_extraction: "Inspect extraction",
  run_rules: "Run the rules",
  compare_documents: "Compare documents",
  retrieve_knowledge: "Retrieve policies",
  analyze: "Analyse",
  determine_confidence: "Assess confidence",
  recommend: "Recommend",
  approval_gate: "Approval gate",
  execute_safe_action: "Request a review",
  propose_for_approval: "Propose for approval",
  finalize: "Finish",
};

export const ACTIVE: AnalysisStatus[] = ["QUEUED", "RUNNING"];
