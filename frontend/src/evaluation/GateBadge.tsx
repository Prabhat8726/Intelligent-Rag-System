import type { EvaluationGateSummary } from "../lib/types";

/** Gate status at a glance: passed, broken (how many) or no gates for this kind of run. */
export function GateBadge({ gates }: { gates: EvaluationGateSummary | null }) {
  if (gates === null) {
    return <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600">No gates</span>;
  }
  if (gates.passed) {
    return (
      <span className="rounded bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-800">
        {gates.checks} {gates.checks === 1 ? "gate" : "gates"} passed
      </span>
    );
  }
  return (
    <span className="rounded bg-red-50 px-2 py-0.5 text-xs font-medium text-red-800">
      {gates.failed} of {gates.checks} gates broken
    </span>
  );
}
