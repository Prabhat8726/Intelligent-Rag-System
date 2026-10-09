import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { Findings, ReviewResolution, ReviewTask, RuleOutcome, RuleResult, Severity } from "../lib/types";
import { OutcomeBadge, PriorityBadge } from "../review/Badges";
import {
  COMPARISON_TYPE_LABELS,
  DUPLICATE_KIND_LABELS,
  ITEM_STATUS_STYLES,
  RESOLUTION_LABELS,
  ROLE_LABELS,
  TASK_TYPE_LABELS,
} from "../review/format";
import { formatDateTime } from "./format";

const NEEDS_REVIEW: ReadonlySet<RuleOutcome> = new Set(["FAIL", "ERROR", "WARN"]);
const OUTCOME_RANK: Record<RuleOutcome, number> = { FAIL: 0, ERROR: 1, WARN: 2, PASS: 3, NOT_APPLICABLE: 4 };
const SEVERITY_RANK: Record<Severity, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };
const HUMAN_RESOLUTIONS: ReviewResolution[] = ["APPROVED", "CORRECTED", "REJECTED"];
const RESOLUTION_HINTS: Record<string, string> = {
  APPROVED: "The findings are acceptable as they are.",
  CORRECTED: "Values were corrected (corrections re-run the checks on their own).",
  REJECTED: "The document must not be processed further. Say why.",
};

function errorText(error: unknown): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : "The request failed.";
}

function byImportance(a: RuleResult, b: RuleResult): number {
  return (
    OUTCOME_RANK[a.outcome] - OUTCOME_RANK[b.outcome] ||
    SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] ||
    a.rule_code.localeCompare(b.rule_code)
  );
}

function historyText(task: ReviewTask): string {
  if (task.status === "CANCELLED") return "cancelled";
  if (task.resolution === "CLEARED" || !task.resolution) return "cleared automatically";
  return `${RESOLUTION_LABELS[task.resolution].toLowerCase()} by ${task.resolved_by?.full_name ?? "a reviewer"}`;
}

function RuleResultRow({ result }: { result: RuleResult }) {
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-1 py-2">
      <OutcomeBadge outcome={result.outcome} />
      <div className="min-w-0 flex-1">
        <p className="text-sm text-slate-800">{result.message}</p>
        <p className="text-xs text-slate-500">
          <span className="font-mono">{result.rule_code}</span> · {result.severity.toLowerCase()}
          {result.comparison_id && (
            <>
              {" · "}
              <Link to={`/comparisons/${result.comparison_id}`} className="text-blue-900 hover:underline">
                view comparison
              </Link>
            </>
          )}
        </p>
      </div>
    </li>
  );
}

function ReviewTaskCard({ task, documentId }: { task: ReviewTask; documentId: string }) {
  const { token, user } = useAuth();
  const queryClient = useQueryClient();
  const canWork = user?.permissions.includes("reviews:work") ?? false;
  const mine = task.assigned_to?.id === user?.id;
  const [resolution, setResolution] = useState<ReviewResolution>("APPROVED");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    setError(null);
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["documents"] }),
      queryClient.invalidateQueries({ queryKey: ["review-tasks"] }),
    ]);
  };
  const act = useMutation({
    mutationFn: (action: "claim" | "release") =>
      apiRequest<ReviewTask>(`/api/v1/review-tasks/${task.id}/${action}`, { method: "POST", token }),
    onSuccess: refresh,
    onError: (failure) => {
      setError(errorText(failure));
    },
  });
  const resolve = useMutation({
    mutationFn: () =>
      apiRequest<ReviewTask>(`/api/v1/review-tasks/${task.id}/resolve`, {
        method: "POST",
        token,
        body: { resolution, note: note.trim() || null },
      }),
    onSuccess: refresh,
    onError: (failure) => {
      setError(errorText(failure));
    },
  });
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (resolution === "REJECTED" && !note.trim()) {
      setError("A rejection needs a note explaining why.");
      return;
    }
    resolve.mutate();
  };

  return (
    <div role="group" aria-labelledby={`task-${documentId}`} className="mt-3 rounded-lg border border-amber-200 bg-amber-50/50 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={`task-${documentId}`} className="text-sm font-medium">
          Open review: {TASK_TYPE_LABELS[task.task_type]}
        </h3>
        <div className="flex items-center gap-2 text-xs text-slate-600">
          <PriorityBadge priority={task.priority} />
          <span className={task.overdue ? "font-medium text-red-700" : undefined}>
            due {formatDateTime(task.due_at)}
            {task.overdue && " (overdue)"}
          </span>
          <span>· {task.assigned_to ? `assigned to ${task.assigned_to.full_name}` : "unassigned"}</span>
        </div>
      </div>
      <ul className="mt-2 list-disc space-y-0.5 pl-5 text-sm text-slate-800">
        {task.reasons.map((reason) => (
          <li key={reason.key}>{reason.message}</li>
        ))}
      </ul>

      {canWork && (
        <div className="mt-3 flex flex-wrap items-end gap-3">
          {!task.assigned_to && (
            <button
              type="button"
              disabled={act.isPending}
              onClick={() => {
                act.mutate("claim");
              }}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-100"
            >
              Claim
            </button>
          )}
          {mine && (
            <button
              type="button"
              disabled={act.isPending}
              onClick={() => {
                act.mutate("release");
              }}
              className="rounded-md px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100"
            >
              Release
            </button>
          )}
          <form aria-label="Resolve review" onSubmit={submit} className="flex flex-wrap items-end gap-3">
            <label className="text-xs">
              <span className="block text-slate-500">Decision</span>
              <select
                value={resolution}
                onChange={(event) => {
                  setResolution(event.target.value as ReviewResolution);
                }}
                className="mt-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-sm"
              >
                {HUMAN_RESOLUTIONS.map((value) => (
                  <option key={value} value={value}>
                    {RESOLUTION_LABELS[value]}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs">
              <span className="block text-slate-500">Note{resolution === "REJECTED" ? "" : " (optional)"}</span>
              <input
                value={note}
                maxLength={1000}
                onChange={(event) => {
                  setNote(event.target.value);
                }}
                className="mt-1 w-64 rounded-md border border-slate-300 bg-white px-2 py-1 text-sm"
              />
            </label>
            <button
              type="submit"
              disabled={resolve.isPending}
              className="rounded-md bg-blue-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              Resolve
            </button>
            <span className="w-full text-xs text-slate-500">{RESOLUTION_HINTS[resolution]}</span>
          </form>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-2 text-sm text-red-700">
          {error}
        </p>
      )}
    </div>
  );
}

export function FindingsSection({ documentId }: { documentId: string }) {
  const { token } = useAuth();
  const findings = useQuery({
    queryKey: ["documents", "findings", documentId],
    queryFn: ({ signal }) => apiRequest<Findings>(`/api/v1/documents/${documentId}/findings`, { token, signal }),
  });

  if (findings.isPending) return null;
  if (findings.isError) {
    return (
      <section aria-labelledby="findings-heading" className="rounded-xl border border-slate-200 bg-white p-5">
        <h2 id="findings-heading" className="font-medium">
          Checks and review
        </h2>
        <p role="alert" className="mt-2 text-sm text-red-700">
          The checks could not be loaded.
        </p>
      </section>
    );
  }

  const data = findings.data;
  const results = [...data.rule_results].sort(byImportance);
  const attention = results.filter((result) => NEEDS_REVIEW.has(result.outcome));
  const passed = results.filter((result) => !NEEDS_REVIEW.has(result.outcome));
  const empty =
    results.length === 0 && data.comparisons.length === 0 && data.duplicates.length === 0 && !data.open_task;

  return (
    <section aria-labelledby="findings-heading" className="rounded-xl border border-slate-200 bg-white p-5">
      <h2 id="findings-heading" className="font-medium">
        Checks and review
      </h2>
      {empty && data.review_history.length === 0 && (
        <p className="mt-2 text-sm text-slate-500">No checks apply to this document.</p>
      )}

      {data.open_task && <ReviewTaskCard task={data.open_task} documentId={documentId} />}

      {data.duplicates.length > 0 && (
        <div className="mt-4">
          <h3 className="text-xs uppercase text-slate-500">Possible duplicates</h3>
          <ul className="mt-1 space-y-1 text-sm">
            {data.duplicates.map((duplicate) => (
              <li key={`${duplicate.kind}-${duplicate.document_id}`}>
                {duplicate.direction === "original" ? "Copy of " : "Copied by "}
                <Link to={`/documents/${duplicate.document_id}`} className="text-blue-900 hover:underline">
                  {duplicate.display_filename}
                </Link>
                <span className="text-xs text-slate-500">
                  {" "}
                  · {DUPLICATE_KIND_LABELS[duplicate.kind] ?? duplicate.kind}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {data.comparisons.length > 0 && (
        <div className="mt-4">
          <h3 className="text-xs uppercase text-slate-500">Comparisons</h3>
          <ul className="mt-1 divide-y divide-slate-100">
            {data.comparisons.map((comparison) => (
              <li key={comparison.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <div className="text-sm">
                  <Link to={`/comparisons/${comparison.id}`} className="font-medium text-blue-900 hover:underline">
                    {COMPARISON_TYPE_LABELS[comparison.comparison_type]}
                  </Link>
                  <span className="block text-xs text-slate-500">
                    {comparison.documents
                      .filter((document) => document.document_id !== documentId)
                      .map((document) => `${ROLE_LABELS[document.role]}: ${document.display_filename}`)
                      .join(" · ")}
                    {comparison.origin === "MANUAL" && " · requested manually"}
                  </span>
                </div>
                <div className="flex gap-1 text-xs">
                  {(["MISMATCH", "UNCERTAIN", "MISSING", "MATCH"] as const).map((status) =>
                    comparison.summary[status] ? (
                      <span key={status} className={`rounded-full px-2 py-0.5 ${ITEM_STATUS_STYLES[status]}`}>
                        {comparison.summary[status]} {status.toLowerCase()}
                      </span>
                    ) : null,
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {results.length > 0 && (
        <div className="mt-4">
          <h3 className="text-xs uppercase text-slate-500">Business rules</h3>
          {attention.length > 0 && (
            <ul aria-label="Rules needing attention" className="divide-y divide-slate-100">
              {attention.map((result) => (
                <RuleResultRow key={result.id} result={result} />
              ))}
            </ul>
          )}
          {passed.length > 0 && (
            <details className="mt-1 text-sm">
              <summary className="cursor-pointer text-slate-600">
                {passed.filter((result) => result.outcome === "PASS").length} passed,{" "}
                {passed.filter((result) => result.outcome === "NOT_APPLICABLE").length} not applicable
              </summary>
              <ul className="divide-y divide-slate-100">
                {passed.map((result) => (
                  <RuleResultRow key={result.id} result={result} />
                ))}
              </ul>
            </details>
          )}
        </div>
      )}

      {data.review_history.length > 0 && (
        <div className="mt-4">
          <h3 className="text-xs uppercase text-slate-500">Review history</h3>
          <ul className="mt-1 space-y-1 text-sm text-slate-700">
            {data.review_history.map((task) => (
              <li key={task.id}>
                {TASK_TYPE_LABELS[task.task_type]}: {historyText(task)}{" "}
                <span className="text-xs text-slate-500">{formatDateTime(task.resolved_at)}</span>
                {task.resolution_note && <span className="block text-xs text-slate-500">“{task.resolution_note}”</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
