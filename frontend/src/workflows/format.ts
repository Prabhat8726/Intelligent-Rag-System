import type { ActionRisk, ActionStatus, StepStatus, WorkflowStatus, WorkflowType } from "../lib/types";

export const WORKFLOW_TYPE_LABELS: Record<WorkflowType, string> = {
  INVOICE_PROCESSING: "Invoice processing",
  CONTRACT_REVIEW: "Contract review",
};

export const WORKFLOW_STATUS_LABELS: Record<WorkflowStatus, string> = {
  QUEUED: "Queued",
  RUNNING: "Running",
  AWAITING_APPROVAL: "Awaiting approval",
  COMPLETED: "Completed",
  REJECTED: "Proposal rejected",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

export const WORKFLOW_STATUS_STYLES: Record<WorkflowStatus, string> = {
  QUEUED: "bg-slate-100 text-slate-700",
  RUNNING: "bg-blue-50 text-blue-800",
  AWAITING_APPROVAL: "bg-amber-50 text-amber-800",
  COMPLETED: "bg-emerald-50 text-emerald-700",
  REJECTED: "bg-orange-50 text-orange-800",
  FAILED: "bg-red-50 text-red-700",
  CANCELLED: "bg-slate-100 text-slate-500",
};

export const ACTIVE_WORKFLOW: WorkflowStatus[] = ["QUEUED", "RUNNING"];

export const OUTCOME_LABELS: Record<string, string> = {
  APPROVED_FOR_PAYMENT: "Approved for payment",
  REJECTED_AS_DUPLICATE: "Rejected as a duplicate",
  AWAITING_VENDOR_CLARIFICATION: "Waiting for the vendor",
  SENT_TO_REVIEW: "Sent to review",
  CONTRACT_APPROVED: "Contract approved",
  SENT_TO_LEGAL_REVIEW: "Sent to Legal review",
  NO_ACTION: "No action needed",
  PROPOSAL_REJECTED: "Proposal rejected",
  ACTION_FAILED: "Action failed",
  WORKFLOW_FAILED: "Workflow failed",
  CANCELLED: "Cancelled",
};

export const STEP_LABELS: Record<string, string> = {
  check_document: "Check the document",
  compare_versions: "Compare with the previous version",
  investigate: "Investigate",
  propose_action: "Propose an action",
  approval: "Human approval",
  execute_action: "Carry out the action",
  report: "Write the report",
};

export const STEP_STYLES: Record<StepStatus, string> = {
  PENDING: "border-slate-200 bg-white text-slate-500",
  RUNNING: "border-blue-300 bg-blue-50 text-blue-900",
  COMPLETED: "border-emerald-300 bg-emerald-50 text-emerald-900",
  FAILED: "border-red-300 bg-red-50 text-red-800",
  SKIPPED: "border-slate-200 bg-slate-50 text-slate-400",
};

export const ACTION_STATUS_STYLES: Record<ActionStatus, string> = {
  PROPOSED: "bg-slate-100 text-slate-700",
  AWAITING_APPROVAL: "bg-amber-50 text-amber-800",
  APPROVED: "bg-blue-50 text-blue-800",
  REJECTED: "bg-orange-50 text-orange-800",
  EXECUTED: "bg-emerald-50 text-emerald-700",
  FAILED: "bg-red-50 text-red-700",
};

export const RISK_STYLES: Record<ActionRisk, string> = {
  LOW: "bg-slate-100 text-slate-700",
  MEDIUM: "bg-amber-50 text-amber-800",
  HIGH: "bg-red-50 text-red-700",
};

export function outcomeLabel(outcome: string | null): string | null {
  return outcome ? (OUTCOME_LABELS[outcome] ?? outcome) : null;
}

export function when(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}
