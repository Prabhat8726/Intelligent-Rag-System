import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { DocumentDetail, Report, ReportPage, ReportType, Workflow, WorkflowPage, WorkflowType } from "../lib/types";
import { REPORT_TYPE_LABELS, documentReportTypes } from "../reports/format";
import { WORKFLOW_STATUS_LABELS, WORKFLOW_STATUS_STYLES, WORKFLOW_TYPE_LABELS, outcomeLabel } from "../workflows/format";

const WORKFLOW_FOR_TYPE: Partial<Record<string, WorkflowType>> = {
  INVOICE: "INVOICE_PROCESSING",
  CONTRACT: "CONTRACT_REVIEW",
};

function errorText(error: unknown): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : "The request failed.";
}

/** The document's workflows and reports, and the buttons that start them. */
export function WorkflowsSection({ document }: { document: DocumentDetail }) {
  const { token, user } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const can = (permission: string) => user?.permissions.includes(permission) ?? false;
  const reportTypes = documentReportTypes(document.document_type);
  const [reportType, setReportType] = useState<ReportType>(reportTypes[0] ?? "COMPLIANCE_REVIEW");
  const workflowType = document.document_type ? WORKFLOW_FOR_TYPE[document.document_type] : undefined;
  const processed = document.status === "COMPLETED" || document.status === "REVIEW_REQUIRED";
  const workflows = useQuery({
    queryKey: ["workflows", "document", document.id],
    queryFn: () => apiRequest<WorkflowPage>(`/api/v1/workflows?document_id=${document.id}&limit=10`, { token }),
    enabled: can("workflows:read"),
  });
  const reports = useQuery({
    queryKey: ["reports", "document", document.id],
    queryFn: () => apiRequest<ReportPage>(`/api/v1/reports?document_id=${document.id}&limit=10`, { token }),
    enabled: can("reports:read"),
  });
  const start = useMutation({
    mutationFn: () =>
      apiRequest<Workflow>("/api/v1/workflows", {
        method: "POST",
        token,
        body: { workflow_type: workflowType, document_id: document.id },
      }),
    onSuccess: (workflow) => {
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
      void navigate(`/workflows/${workflow.id}`);
    },
  });
  const generate = useMutation({
    mutationFn: () =>
      apiRequest<Report>("/api/v1/reports", {
        method: "POST",
        token,
        body: { report_type: reportType, subject_id: document.id },
      }),
    onSuccess: (report) => {
      void queryClient.invalidateQueries({ queryKey: ["reports"] });
      void navigate(`/reports/${report.id}`);
    },
  });
  if (!can("workflows:read") && !can("reports:read")) return null;
  const error = start.error ?? generate.error;
  return (
    <section aria-labelledby="workflows-heading" className="rounded-xl border border-slate-200 bg-white p-5">
      <h2 id="workflows-heading" className="font-medium">
        Workflows and reports
      </h2>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {workflowType && can("workflows:start") && (
          <button
            type="button"
            disabled={!processed || start.isPending}
            onClick={() => {
              start.mutate();
            }}
            className="rounded-md bg-blue-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-800 disabled:opacity-50"
          >
            Start {WORKFLOW_TYPE_LABELS[workflowType].toLowerCase()}
          </button>
        )}
        {can("reports:create") && processed && (
          <>
            <select
              aria-label="Report type"
              value={reportType}
              onChange={(event) => {
                setReportType(event.target.value as ReportType);
              }}
              className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
            >
              {reportTypes.map((value) => (
                <option key={value} value={value}>
                  {REPORT_TYPE_LABELS[value]}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={generate.isPending}
              onClick={() => {
                generate.mutate();
              }}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-50"
            >
              Generate report
            </button>
          </>
        )}
      </div>
      {error && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {errorText(error)}
        </p>
      )}
      {workflows.data && workflows.data.items.length > 0 && (
        <ul aria-label="Document workflows" className="mt-4 space-y-1 text-sm">
          {workflows.data.items.map((item) => (
            <li key={item.id} className="flex flex-wrap items-center gap-2">
              <Link to={`/workflows/${item.id}`} className="text-blue-900 hover:underline">
                {WORKFLOW_TYPE_LABELS[item.workflow_type]} (version {item.document.version_number ?? "?"})
              </Link>
              <span className={`rounded px-2 py-0.5 text-xs ${WORKFLOW_STATUS_STYLES[item.status]}`}>
                {WORKFLOW_STATUS_LABELS[item.status]}
              </span>
              {item.pending_action && (
                <span className="text-xs text-slate-600">
                  {item.pending_action.title}: waiting for a {item.pending_action.required_role?.toLowerCase()}
                </span>
              )}
              {!item.pending_action && item.outcome && (
                <span className="text-xs text-slate-600">{outcomeLabel(item.outcome)}</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {reports.data && reports.data.items.length > 0 && (
        <ul aria-label="Document reports" className="mt-3 space-y-1 text-sm">
          {reports.data.items.map((item) => (
            <li key={item.id}>
              <Link to={`/reports/${item.id}`} className="text-blue-900 hover:underline">
                {REPORT_TYPE_LABELS[item.report_type]}
              </Link>{" "}
              <span className="text-xs text-slate-500">as of {new Date(item.as_of).toLocaleString()}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
