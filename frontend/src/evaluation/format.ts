import type { EvaluationGateCheck } from "../lib/types";

export function shortRevision(revision: string | null): string {
  return revision ? revision.slice(0, 7) + (revision.endsWith("+dirty") ? " (uncommitted)" : "") : "unknown commit";
}

export function formatDate(value: string): string {
  return new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function bound(check: EvaluationGateCheck): string {
  if (check.min !== null && check.max !== null) {
    return check.min === check.max ? `= ${String(check.min)}` : `${String(check.min)} – ${String(check.max)}`;
  }
  return check.min !== null ? `≥ ${String(check.min)}` : `≤ ${String(check.max)}`;
}
