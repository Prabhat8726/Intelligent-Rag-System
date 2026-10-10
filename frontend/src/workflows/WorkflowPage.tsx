import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { Findings, SourceItem } from "../analysis/AnalysisRunPage";
import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { Workflow, WorkflowAction } from "../lib/types";
import {
  ACTION_STATUS_STYLES,
  ACTIVE_WORKFLOW,
  RISK_STYLES,
  STEP_LABELS,
  STEP_STYLES,
  WORKFLOW_STATUS_LABELS,
  WORKFLOW_STATUS_STYLES,
  outcomeLabel,
  when,
} from "./format";

function errorText(error: unknown): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : "The request failed.";
}

function asText(value: unknown): string | null {
  return typeof value === "string" || typeof value === "number" ? String(value) : null;
}

function Subject({ payload }: { payload: Record<string, unknown> }) {
  const subject = (payload.document ?? {}) as Record<string, unknown>;
  const facts = [
    asText(subject.document_number),
    asText(subject.vendor_name),
    asText(subject.document_date),
    asText(subject.total) ? `${asText(subject.total) ?? ""} ${asText(subject.currency) ?? ""}`.trim() : null,
  ].filter(Boolean);
  const issues = Array.isArray(payload.issues) ? (payload.issues as string[]) : [];
  const changes = payload.version_changes as { from_version?: number; to_version?: number; changed?: number } | undefined;
  return (
    <div className="mt-3 space-y-2 text-sm">
      {facts.length > 0 && <p className="text-slate-700">{facts.join(" · ")}</p>}
      {issues.length > 0 && (
        <ul aria-label="Issues" className="list-disc space-y-1 pl-5 text-slate-700">
          {issues.map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      )}
      {changes?.changed ? (
        <p className="text-slate-700">
          Version {changes.to_version} changed {changes.changed} clause(s) since version {changes.from_version}.
        </p>
      ) : null}
    </div>
  );
}

function Decision({ workflow, action }: { workflow: Workflow; action: WorkflowAction }) {
  const { token } = useAuth();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const decide = useMutation({
    mutationFn: (verb: "approve" | "reject") =>
      apiRequest<Workflow>(`/api/v1/workflows/${workflow.id}/${verb}`, {
        method: "POST",
        token,
        body: reason.trim() ? { reason: reason.trim() } : {},
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["workflow", workflow.id], updated);
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
      void queryClient.invalidateQueries({ queryKey: ["workflow-counts"] });
    },
  });
  if (!action.can_decide) {
    return (
      <div className="mt-4 rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-700" aria-label="Why you cannot decide">
        <p className="font-medium">Waiting for a {action.required_role?.toLowerCase() ?? "person"}.</p>
        <ul className="mt-1 list-disc pl-5">
          {action.blockers.map((blocker) => (
            <li key={blocker}>{blocker}</li>
          ))}
        </ul>
      </div>
    );
  }
  return (
    <form
      aria-label="Decision"
      className="mt-4 space-y-2"
      onSubmit={(event) => {
        event.preventDefault();
      }}
    >
      <label className="block text-sm">
        <span className="text-slate-700">Reason (required to reject)</span>
        <textarea
          value={reason}
          maxLength={1000}
          rows={2}
          onChange={(event) => {
            setReason(event.target.value);
          }}
          className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
      </label>
      {decide.error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {errorText(decide.error)}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          disabled={decide.isPending}
          onClick={() => {
            decide.mutate("approve");
          }}
          className="rounded-md bg-emerald-700 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
        >
          Approve
        </button>
        <button
          type="button"
          disabled={decide.isPending || reason.trim().length < 3}
          onClick={() => {
            decide.mutate("reject");
          }}
          className="rounded-md border border-red-300 px-4 py-2 text-sm font-medium text-red-800 hover:bg-red-50 disabled:opacity-50"
        >
          Reject
        </button>
      </div>
    </form>
  );
}

function Result({ result }: { result: Record<string, unknown> }) {
  const message = asText(result.message);
  return (
    <div aria-label="Result" className="mt-3 space-y-1 text-sm text-slate-800">
      {asText(result.payment_reference) && <p>Payment reference: {asText(result.payment_reference)}</p>}
      {asText(result.note) && <p className="text-slate-600">{asText(result.note)}</p>}
      {asText(result.review_task_id) && (
        <p>
          <Link to="/reviews" className="text-blue-900 underline">
            Open the review queue
          </Link>
        </p>
      )}
      {message && (
        <details className="rounded-md border border-slate-200 bg-slate-50 p-3">
          <summary className="cursor-pointer text-sm font-medium">Message for the vendor (not sent)</summary>
          <pre className="mt-2 whitespace-pre-wrap font-sans text-sm">{message}</pre>
        </details>
      )}
    </div>
  );
}

function ActionCard({ workflow, action }: { workflow: Workflow; action: WorkflowAction }) {
  return (
    <section aria-label="Proposed action" className="rounded-xl border border-slate-200 bg-white p-5">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-semibold">{action.title}</h2>
        <span className={`rounded px-2 py-0.5 text-xs ${ACTION_STATUS_STYLES[action.status]}`}>
          {action.status.replace("_", " ").toLowerCase()}
        </span>
        <span className={`rounded px-2 py-0.5 text-xs ${RISK_STYLES[action.risk_level]}`}>
          risk {action.risk_level.toLowerCase()}
        </span>
        <span className="text-xs text-slate-500">
          {action.requires_approval
            ? `needs a ${action.required_role?.toLowerCase() ?? "person"}'s approval`
            : "low risk: carried out without approval"}
          {" · "}
          {action.proposed_by_type === "AGENT" ? "proposed by the AI model, allowed by the guardrails" : "proposed by the rules"}
          {action.confidence_level && ` · confidence ${action.confidence_level.toLowerCase()}`}
        </span>
      </div>
      <p className="mt-2 text-sm text-slate-800">{action.rationale}</p>
      <Subject payload={action.payload} />
      {action.status === "AWAITING_APPROVAL" && <Decision workflow={workflow} action={action} />}
      {action.decided_by && (
        <p className="mt-3 text-sm">
          <span className="font-medium">{action.status === "REJECTED" ? "Rejected" : "Approved"}</span> by{" "}
          {action.decided_by.full_name} on {when(action.decided_at)}
          {action.decision_reason && `: ${action.decision_reason}`}
        </p>
      )}
      {action.execution_result && <Result result={action.execution_result} />}
      {action.error && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {action.error}
        </p>
      )}
      <details className="mt-4 text-sm">
        <summary className="cursor-pointer text-slate-700">History ({action.transitions.length})</summary>
        <table className="mt-2 w-full text-left text-xs" aria-label="Action history">
          <thead className="text-slate-500">
            <tr>
              <th className="py-1 font-medium">When</th>
              <th className="font-medium">Change</th>
              <th className="font-medium">By</th>
              <th className="font-medium">Reason</th>
            </tr>
          </thead>
          <tbody>
            {action.transitions.map((item) => (
              <tr key={`${item.to_status}-${item.created_at}`} className="align-top">
                <td className="py-1 pr-2">{when(item.created_at)}</td>
                <td className="pr-2">
                  {item.from_status ?? "new"} → {item.to_status}
                </td>
                <td className="pr-2">
                  {item.actor_type === "USER"
                    ? (item.actor?.full_name ?? "—")
                    : `${item.actor_type.toLowerCase()}${item.actor ? ` for ${item.actor.full_name}` : ""}`}
                </td>
                <td>{item.reason ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </section>
  );
}

export function WorkflowPage() {
  const { workflowId } = useParams();
  const { token, user } = useAuth();
  const queryClient = useQueryClient();
  const workflow = useQuery({
    queryKey: ["workflow", workflowId],
    queryFn: () => apiRequest<Workflow>(`/api/v1/workflows/${workflowId ?? ""}`, { token }),
    refetchInterval: (query) => (query.state.data && ACTIVE_WORKFLOW.includes(query.state.data.status) ? 1500 : false),
  });
  const cancel = useMutation({
    mutationFn: () => apiRequest<Workflow>(`/api/v1/workflows/${workflowId ?? ""}/cancel`, { method: "POST", token, body: {} }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["workflow", workflowId], updated);
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
  });
  if (workflow.error) {
    return (
      <p role="alert" className="text-sm text-red-700">
        {errorText(workflow.error)}
      </p>
    );
  }
  if (!workflow.data) return <p className="text-sm text-slate-500">Loading…</p>;
  const data = workflow.data;
  const mayCancel =
    ACTIVE_WORKFLOW.includes(data.status) &&
    (data.initiated_by.id === user?.id || user?.role === "MANAGER" || user?.role === "ADMIN");
  const analysis = data.analysis;
  return (
    <div className="space-y-6">
      <div>
        <Link to="/workflows" className="text-sm text-blue-900 hover:underline">
          ← Workflows
        </Link>
        <h1 className="mt-1 text-xl font-semibold">
          {data.title}: {data.document.filename}
        </h1>
        <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
          <span className={`rounded px-2 py-0.5 text-xs ${WORKFLOW_STATUS_STYLES[data.status]}`}>
            {WORKFLOW_STATUS_LABELS[data.status]}
          </span>
          {data.outcome && data.status !== "AWAITING_APPROVAL" && (
            <span className="text-slate-700">{outcomeLabel(data.outcome)}</span>
          )}
          <Link to={`/documents/${data.document.id}`} className="text-blue-900 hover:underline">
            Open the document
          </Link>
          <span className="text-slate-500">
            version {data.document.version_number ?? "?"}
            {!data.document.is_current_version && " (a newer version exists)"} · started by {data.initiated_by.full_name}
            {data.trigger === "AUTO" ? " (automatically)" : ""} · {when(data.created_at)}
          </span>
        </div>
      </div>
      {data.error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {data.error}
        </p>
      )}
      <ol aria-label="Steps" className="flex flex-wrap gap-2">
        {data.steps.map((step) => (
          <li key={step.sequence} className={`rounded-md border px-3 py-1.5 text-xs ${STEP_STYLES[step.status]}`}>
            <span className="font-medium">{STEP_LABELS[step.step_name] ?? step.step_name}</span>
            <span className="ml-1">
              {step.status === "RUNNING" && step.step_name === "approval" ? "waiting" : step.status.toLowerCase()}
            </span>
          </li>
        ))}
      </ol>
      {mayCancel && (
        <div>
          <button
            type="button"
            onClick={() => {
              cancel.mutate();
            }}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100"
          >
            Cancel workflow
          </button>
          {cancel.error && (
            <p role="alert" className="mt-2 text-sm text-red-700">
              {errorText(cancel.error)}
            </p>
          )}
        </div>
      )}
      {data.actions.map((action) => (
        <ActionCard key={action.id} workflow={data} action={action} />
      ))}
      {data.report_ids.length > 0 && (
        <section aria-label="Reports" className="text-sm">
          <h2 className="font-semibold text-slate-700">Report</h2>
          <ul className="mt-1">
            {data.report_ids.map((id) => (
              <li key={id}>
                <Link to={`/reports/${id}`} className="text-blue-900 hover:underline">
                  Open the report
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
      {analysis && (
        <section aria-label="Investigation" className="space-y-4">
          <div>
            <h2 className="text-sm font-semibold text-slate-700">What the investigation found</h2>
            <p className="mt-1 text-sm text-slate-900">{analysis.summary}</p>
            <p className="text-xs text-slate-500">
              Confidence {analysis.confidence.level.toLowerCase()} ({Math.round(analysis.confidence.score * 100)}%)
              {data.agent_run_id && (data.initiated_by.id === user?.id || user?.role === "ADMIN") && (
                <>
                  {" · "}
                  <Link to={`/analysis/${data.agent_run_id}`} className="text-blue-900 hover:underline">
                    open the investigation
                  </Link>
                </>
              )}
            </p>
          </div>
          <Findings result={analysis} />
          {analysis.sources.length > 0 && (
            <section aria-label="Sources">
              <h2 className="text-sm font-semibold text-slate-700">Policy sources</h2>
              <ol className="mt-2 space-y-2">
                {analysis.sources.map((source) => (
                  <SourceItem key={source.chunk_id} source={source} />
                ))}
              </ol>
            </section>
          )}
        </section>
      )}
    </div>
  );
}
