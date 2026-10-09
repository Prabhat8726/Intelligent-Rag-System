import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth/useAuth";
import { DOCUMENT_TYPE_LABELS, formatDateTime } from "../documents/format";
import { ApiError, apiRequest } from "../lib/api";
import type { Page, ReviewTaskListItem, ReviewTaskType } from "../lib/types";
import { PriorityBadge } from "./Badges";
import { REVIEW_PAGE_SIZE as PAGE_SIZE, reviewTasksUrl, TASK_TYPE_LABELS } from "./format";

export function ReviewQueuePage() {
  const { token, user } = useAuth();
  const queryClient = useQueryClient();
  const [state, setState] = useState("open");
  const [taskType, setTaskType] = useState("");
  const [assigned, setAssigned] = useState("any");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const url = reviewTasksUrl({ state, taskType, assigned, offset });
  const tasks = useQuery({
    queryKey: ["review-tasks", url],
    queryFn: ({ signal }) => apiRequest<Page<ReviewTaskListItem>>(url, { token, signal }),
  });
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "claim" | "release" }) =>
      apiRequest<ReviewTaskListItem>(`/api/v1/review-tasks/${id}/${action}`, { method: "POST", token }),
    onSuccess: async () => {
      setError(null);
      await queryClient.invalidateQueries({ queryKey: ["review-tasks"] });
    },
    onError: (failure) => {
      setError(failure instanceof ApiError ? (failure.problem?.detail ?? failure.message) : "The request failed.");
    },
  });

  const select = (label: string, value: string, onChange: (value: string) => void, options: [string, string][]) => (
    <label className="text-sm">
      <span className="sr-only">{label}</span>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
          setOffset(0);
        }}
        className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
      >
        {options.map(([optionValue, optionLabel]) => (
          <option key={optionValue} value={optionValue}>
            {optionLabel}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Review queue</h1>
        <p className="mt-1 text-sm text-slate-500">
          Documents a person has to look at: uncertain extraction, discrepancies against the order or delivery,
          possible duplicates. Most urgent first.
        </p>
      </div>

      <div className="flex flex-wrap gap-3">
        {select("Task state", state, setState, [
          ["open", "Open"],
          ["closed", "Closed"],
          ["all", "All"],
        ])}
        {select("Task type", taskType, setTaskType, [
          ["", "All types"],
          ...(Object.entries(TASK_TYPE_LABELS) as [ReviewTaskType, string][]),
        ])}
        {select("Assigned", assigned, setAssigned, [
          ["any", "Anyone"],
          ["me", "Assigned to me"],
          ["unassigned", "Unassigned"],
        ])}
      </div>

      {error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}

      <section className="rounded-xl border border-slate-200 bg-white">
        {tasks.isPending ? (
          <p className="p-5 text-sm text-slate-500">Loading…</p>
        ) : tasks.isError ? (
          <p role="alert" className="p-5 text-sm text-red-700">
            The review queue could not be loaded.
          </p>
        ) : tasks.data.items.length === 0 ? (
          <p className="p-5 text-sm text-slate-500">Nothing to review.</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-xs uppercase text-slate-500">
              <tr>
                <th className="px-5 py-2 font-medium">Document</th>
                <th className="py-2 font-medium">Priority</th>
                <th className="py-2 font-medium">Why</th>
                <th className="py-2 font-medium">Due</th>
                <th className="py-2 font-medium">Assigned</th>
                <th className="py-2 pr-5" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {tasks.data.items.map((task) => {
                const open = task.status === "OPEN" || task.status === "IN_PROGRESS";
                const mine = task.assigned_to?.id === user?.id;
                return (
                  <tr key={task.id} className="align-top">
                    <td className="px-5 py-2.5">
                      <Link to={`/documents/${task.document.id}`} className="font-medium text-blue-900 hover:underline">
                        {task.document.display_filename}
                      </Link>
                      <span className="block text-xs text-slate-500">
                        {task.document.document_type ? DOCUMENT_TYPE_LABELS[task.document.document_type] : "Unclassified"} ·{" "}
                        {TASK_TYPE_LABELS[task.task_type]}
                      </span>
                    </td>
                    <td className="py-2.5">
                      <PriorityBadge priority={task.priority} />
                    </td>
                    <td className="max-w-md py-2.5 pr-3">
                      <ul className="space-y-0.5 text-slate-700">
                        {task.reasons.slice(0, 3).map((reason) => (
                          <li key={reason.key}>{reason.message}</li>
                        ))}
                        {task.reasons.length > 3 && (
                          <li className="text-xs text-slate-500">and {task.reasons.length - 3} more</li>
                        )}
                      </ul>
                      {!open && task.resolution && (
                        <span className="mt-1 block text-xs text-slate-500">
                          {task.resolution.toLowerCase()} by {task.resolved_by?.full_name ?? "the system"}
                        </span>
                      )}
                    </td>
                    <td className={`py-2.5 text-xs ${task.overdue ? "font-medium text-red-700" : "text-slate-600"}`}>
                      {formatDateTime(task.due_at)}
                      {task.overdue && <span className="block">overdue</span>}
                    </td>
                    <td className="py-2.5 text-xs text-slate-600">{task.assigned_to?.full_name ?? "—"}</td>
                    <td className="py-2.5 pr-5 text-right whitespace-nowrap">
                      {open && !task.assigned_to && (
                        <button
                          type="button"
                          onClick={() => {
                            act.mutate({ id: task.id, action: "claim" });
                          }}
                          className="rounded-md border border-slate-300 px-2.5 py-1 text-xs hover:bg-slate-100"
                        >
                          Claim
                        </button>
                      )}
                      {open && mine && (
                        <button
                          type="button"
                          onClick={() => {
                            act.mutate({ id: task.id, action: "release" });
                          }}
                          className="rounded-md px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-100"
                        >
                          Release
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {tasks.data && tasks.data.total > PAGE_SIZE && (
          <div className="flex items-center justify-between border-t border-slate-200 px-5 py-3 text-sm">
            <span>
              {offset + 1}–{Math.min(offset + PAGE_SIZE, tasks.data.total)} of {tasks.data.total}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={offset === 0}
                onClick={() => {
                  setOffset(Math.max(0, offset - PAGE_SIZE));
                }}
                className="rounded-md border border-slate-300 px-3 py-1 disabled:opacity-40"
              >
                Previous
              </button>
              <button
                type="button"
                disabled={offset + PAGE_SIZE >= tasks.data.total}
                onClick={() => {
                  setOffset(offset + PAGE_SIZE);
                }}
                className="rounded-md border border-slate-300 px-3 py-1 disabled:opacity-40"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
