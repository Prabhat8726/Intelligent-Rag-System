import type {
  ClauseDiff,
  ComparisonRole,
  ComparisonType,
  ItemStatus,
  ReviewPriority,
  ReviewResolution,
  ReviewTaskType,
  RuleOutcome,
  Severity,
} from "../lib/types";

type ClauseChange = ClauseDiff["change"];

export const ITEM_STATUS_STYLES: Record<ItemStatus, string> = {
  MATCH: "bg-emerald-50 text-emerald-700",
  MISMATCH: "bg-red-50 text-red-700",
  MISSING: "bg-slate-100 text-slate-700",
  UNCERTAIN: "bg-amber-50 text-amber-800",
};

export const OUTCOME_LABELS: Record<RuleOutcome, string> = {
  PASS: "Passed",
  FAIL: "Failed",
  WARN: "Check",
  ERROR: "Error",
  NOT_APPLICABLE: "Not applicable",
};

export const OUTCOME_STYLES: Record<RuleOutcome, string> = {
  PASS: "bg-emerald-50 text-emerald-700",
  FAIL: "bg-red-50 text-red-700",
  WARN: "bg-amber-50 text-amber-800",
  ERROR: "bg-red-100 text-red-800",
  NOT_APPLICABLE: "bg-slate-100 text-slate-600",
};

export const PRIORITY_LABELS: Record<ReviewPriority, string> = {
  URGENT: "Urgent",
  HIGH: "High",
  NORMAL: "Normal",
  LOW: "Low",
};

export const PRIORITY_STYLES: Record<ReviewPriority, string> = {
  URGENT: "bg-red-600 text-white",
  HIGH: "bg-red-50 text-red-700",
  NORMAL: "bg-amber-50 text-amber-800",
  LOW: "bg-slate-100 text-slate-700",
};

export const TASK_TYPE_LABELS: Record<ReviewTaskType, string> = {
  DUPLICATE_REVIEW: "Possible duplicate",
  DISCREPANCY_REVIEW: "Discrepancy",
  EXTRACTION_REVIEW: "Extraction",
  CLASSIFICATION_REVIEW: "Document type",
};

export const RESOLUTION_LABELS: Record<ReviewResolution, string> = {
  APPROVED: "Approved",
  CORRECTED: "Corrected",
  REJECTED: "Rejected",
  CLEARED: "Cleared automatically",
};

export const SEVERITY_LABELS: Record<Severity, string> = {
  LOW: "Low",
  MEDIUM: "Medium",
  HIGH: "High",
  CRITICAL: "Critical",
};

export const COMPARISON_TYPE_LABELS: Record<ComparisonType, string> = {
  INVOICE_PO: "Invoice ↔ purchase order",
  INVOICE_DELIVERY: "Invoice ↔ delivery notes",
  INVOICE_PO_DELIVERY: "Three-way match (invoice, order, deliveries)",
  PO_DELIVERY: "Delivery note ↔ purchase order",
};

export const ROLE_LABELS: Record<ComparisonRole, string> = {
  INVOICE: "Invoice",
  PURCHASE_ORDER: "Purchase order",
  DELIVERY_NOTE: "Delivery note",
};

export const CHECK_LABELS: Record<string, string> = {
  vendor: "Vendor",
  currency: "Currency",
  po_reference: "Purchase order number",
  tax_rate: "Tax rate",
  payment_terms: "Payment terms",
  unit_price: "Unit price",
  quantity_vs_ordered: "Quantity vs ordered",
  quantity_vs_delivered: "Quantity vs delivered",
  line_on_order: "On the order",
  line_fulfilled: "Ordered item present",
  line_delivered: "Delivered",
};

/** "0.0825" -> "8.25%" for tax rates; other values unchanged. */
export function itemValue(check: string, value: string | null): string {
  if (value === null) return "—";
  if (check === "tax_rate" && !Number.isNaN(Number(value))) {
    return `${String(Math.round(Number(value) * 10000) / 100)}%`;
  }
  return value;
}

export const DUPLICATE_KIND_LABELS: Record<string, string> = {
  SAME_FILE: "Identical file",
  SAME_VENDOR_AND_NUMBER: "Same vendor and document number",
  SAME_VENDOR_AMOUNT_AND_DATE: "Same vendor, amount and close date",
};

export const CHANGE_LABELS: Record<ClauseChange, string> = {
  ADDED: "Added",
  REMOVED: "Removed",
  MODIFIED: "Modified",
  UNCHANGED: "Unchanged",
};

export const CHANGE_STYLES: Record<ClauseChange, string> = {
  ADDED: "bg-emerald-50 text-emerald-700",
  REMOVED: "bg-red-50 text-red-700",
  MODIFIED: "bg-amber-50 text-amber-800",
  UNCHANGED: "bg-slate-100 text-slate-600",
};

export const REVIEW_PAGE_SIZE = 25;

export function reviewTasksUrl(filters: {
  state: string;
  taskType: string;
  assigned: string;
  offset: number;
}): string {
  const params = new URLSearchParams({ state: filters.state, assigned: filters.assigned });
  if (filters.taskType) params.set("task_type", filters.taskType);
  params.set("limit", String(REVIEW_PAGE_SIZE));
  params.set("offset", String(filters.offset));
  return `/api/v1/review-tasks?${params.toString()}`;
}
