import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { WorkflowPage, WorkflowStatus, WorkflowType } from "../lib/types";
import {
  ACTIVE_WORKFLOW,
  WORKFLOW_STATUS_LABELS,
  WORKFLOW_STATUS_STYLES,
  WORKFLOW_TYPE_LABELS,
  outcomeLabel,
  when,
} from "./format";

type View = "mine" | "all";

export function WorkflowsPage() {
  const { token, user } = useAuth();
  const mayDecide = user?.permissions.includes("workflows:approve") ?? false;
  const [view, setView] = useState<View>(mayDecide ? "mine" : "all");
  const [status, setStatus] = useState<WorkflowStatus | "">("");
  const [type, setType] = useState<WorkflowType | "">("");
  const params = new URLSearchParams({ limit: "50" });
  if (view === "mine") params.set("awaiting_me", "true");
  if (status) params.set("status", status);
  if (type) params.set("workflow_type", type);
  const workflows = useQuery({
    queryKey: ["workflows", view, status, type],
    queryFn: () => apiRequest<WorkflowPage>(`/api/v1/workflows?${params.toString()}`, { token }),
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => ACTIVE_WORKFLOW.includes(item.status)) ? 2000 : false,
  });
  const tab = (value: View, label: string) => (
    <button
      type="button"
      role="tab"
      aria-selected={view === value}
      onClick={() => {
        setView(value);
      }}
      className={`rounded-md px-3 py-1.5 text-sm ${
        view === value ? "bg-blue-900 text-white" : "border border-slate-300 text-slate-700 hover:bg-slate-100"
      }`}
    >
      {label}
    </button>
  );
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Workflows</h1>
        <p className="mt-1 text-sm text-slate-600">
          Invoice processing and contract review: each workflow checks a document, investigates it and proposes one
          action. Payments, rejections, vendor clarifications and contract approvals wait for a person who did not
          start the workflow or upload the document. Start one from a document&apos;s page.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <div role="tablist" aria-label="Workflow view" className="flex gap-2">
          {mayDecide && tab("mine", "Awaiting my decision")}
          {tab("all", "All workflows")}
        </div>
        <select
          aria-label="Status"
          value={status}
          onChange={(event) => {
            setStatus(event.target.value as WorkflowStatus | "");
          }}
          className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          <option value="">Any status</option>
          {(Object.keys(WORKFLOW_STATUS_LABELS) as WorkflowStatus[]).map((value) => (
            <option key={value} value={value}>
              {WORKFLOW_STATUS_LABELS[value]}
            </option>
          ))}
        </select>
        <select
          aria-label="Workflow type"
          value={type}
          onChange={(event) => {
            setType(event.target.value as WorkflowType | "");
          }}
          className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          <option value="">Any type</option>
          {(Object.keys(WORKFLOW_TYPE_LABELS) as WorkflowType[]).map((value) => (
            <option key={value} value={value}>
              {WORKFLOW_TYPE_LABELS[value]}
            </option>
          ))}
        </select>
      </div>
      <section aria-label="Workflow list" className="rounded-xl border border-slate-200 bg-white">
        {workflows.isPending ? (
          <p className="p-5 text-sm text-slate-500">Loading…</p>
        ) : workflows.isError ? (
          <p role="alert" className="p-5 text-sm text-red-700">
            {workflows.error instanceof ApiError
              ? (workflows.error.problem?.detail ?? workflows.error.message)
              : "The workflows could not be loaded."}
          </p>
        ) : workflows.data.items.length === 0 ? (
          <p className="p-5 text-sm text-slate-500">
            {view === "mine" ? "Nothing waits for your decision." : "No workflow yet."}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-5 py-2 font-medium">Document</th>
                  <th className="py-2 font-medium">Workflow</th>
                  <th className="py-2 font-medium">Status</th>
                  <th className="py-2 font-medium">Waiting for</th>
                  <th className="py-2 font-medium">Started by</th>
                  <th className="py-2 pr-5 font-medium">Started</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {workflows.data.items.map((item) => (
                  <tr key={item.id} className="align-top">
                    <td className="px-5 py-2.5">
                      <Link to={`/workflows/${item.id}`} className="font-medium text-blue-900 hover:underline">
                        {item.document.filename}
                      </Link>
                      {!item.document.is_current_version && (
                        <span className="block text-xs text-amber-800">a newer version exists</span>
                      )}
                    </td>
                    <td className="py-2.5">{WORKFLOW_TYPE_LABELS[item.workflow_type]}</td>
                    <td className="py-2.5">
                      <span className={`rounded px-2 py-0.5 text-xs ${WORKFLOW_STATUS_STYLES[item.status]}`}>
                        {WORKFLOW_STATUS_LABELS[item.status]}
                      </span>
                      {item.outcome && item.status !== "AWAITING_APPROVAL" && (
                        <span className="block text-xs text-slate-500">{outcomeLabel(item.outcome)}</span>
                      )}
                    </td>
                    <td className="py-2.5">
                      {item.pending_action
                        ? `${item.pending_action.title} (${item.pending_action.required_role?.toLowerCase() ?? "anyone"})`
                        : "—"}
                    </td>
                    <td className="py-2.5">
                      {item.initiated_by.full_name}
                      {item.trigger === "AUTO" && <span className="block text-xs text-slate-500">started automatically</span>}
                    </td>
                    <td className="py-2.5 pr-5 text-slate-600">{when(item.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
