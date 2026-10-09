import type { AnswerStatus, KnowledgeCategory, KnowledgeStatus } from "../lib/types";

export const CATEGORY_LABELS: Record<KnowledgeCategory, string> = {
  POLICY: "Policy",
  PROCEDURE: "Procedure",
  CONTRACT_GUIDELINE: "Contract guideline",
  FAQ: "FAQ",
  COMPLIANCE: "Compliance",
  PUBLIC_REFERENCE: "Public reference",
};
export const CATEGORIES = Object.keys(CATEGORY_LABELS) as KnowledgeCategory[];

const STATUS: Record<KnowledgeStatus, { label: string; style: string }> = {
  PROCESSING: { label: "Processing", style: "bg-blue-50 text-blue-800" },
  ACTIVE: { label: "Active", style: "bg-emerald-50 text-emerald-700" },
  SUPERSEDED: { label: "Superseded", style: "bg-slate-100 text-slate-700" },
  ARCHIVED: { label: "Archived", style: "bg-slate-100 text-slate-500" },
  FAILED: { label: "Failed", style: "bg-red-50 text-red-700" },
};

export function knowledgeStatusLabel(status: KnowledgeStatus): string {
  return STATUS[status].label;
}

export function knowledgeStatusStyle(status: KnowledgeStatus): string {
  return STATUS[status].style;
}

export const ANSWER_STATUS: Record<AnswerStatus, { label: string; style: string; help: string }> = {
  ANSWERED: {
    label: "Answered",
    style: "bg-emerald-50 text-emerald-800",
    help: "Every statement cites a source it was checked against.",
  },
  PARTIALLY_SUPPORTED: {
    label: "Partially supported",
    style: "bg-amber-50 text-amber-800",
    help: "Some statements were removed or could not be matched to their sources.",
  },
  INSUFFICIENT_EVIDENCE: {
    label: "Insufficient evidence",
    style: "bg-slate-100 text-slate-700",
    help: "The knowledge base does not answer this question.",
  },
  RETRIEVAL_ONLY: {
    label: "Passages only",
    style: "bg-blue-50 text-blue-800",
    help: "No answer was generated; read the passages below.",
  },
};

/** "2026-01-01 – 2026-12-31", "from 2026-01-01", "until 2025-12-31" or "" (always). */
export function formatPeriod(from: string | null, to: string | null): string {
  if (from && to) return `${from} – ${to}`;
  if (from) return `from ${from}`;
  if (to) return `until ${to}`;
  return "";
}

export interface KnowledgeFilters {
  status: KnowledgeStatus | "";
  category: KnowledgeCategory | "";
  q: string;
  offset: number;
}

export const PAGE_SIZE = 50;

export function knowledgeUrl(filters: KnowledgeFilters): string {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(filters.offset) });
  if (filters.status) params.set("status", filters.status);
  if (filters.category) params.set("category", filters.category);
  if (filters.q.trim()) params.set("q", filters.q.trim());
  return `/api/v1/knowledge/documents?${params.toString()}`;
}
