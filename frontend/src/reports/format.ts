import type { DocumentType, ReportType } from "../lib/types";

export const REPORT_TYPE_LABELS: Record<ReportType, string> = {
  INVOICE_VERIFICATION: "Invoice verification",
  CONTRACT_REVIEW: "Contract review",
  DOCUMENT_COMPARISON: "Document comparison",
  COMPLIANCE_REVIEW: "Compliance review",
  AI_ANALYSIS: "AI analysis",
};

/** The document reports that apply to a type (comparison and analysis reports start elsewhere). */
export function documentReportTypes(type: DocumentType | null): ReportType[] {
  if (type === "INVOICE") return ["INVOICE_VERIFICATION", "COMPLIANCE_REVIEW"];
  if (type === "CONTRACT") return ["CONTRACT_REVIEW", "COMPLIANCE_REVIEW"];
  return ["COMPLIANCE_REVIEW"];
}
