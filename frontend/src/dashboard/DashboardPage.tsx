import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { ACTION_LABELS } from "../analysis/format";
import { useAuth } from "../auth/useAuth";
import { DOCUMENT_TYPE_LABELS, statusLabel } from "../documents/format";
import { ApiError, apiRequest } from "../lib/api";
import type { DashboardSummary, DocumentStatus } from "../lib/types";
import { outcomeLabel } from "../workflows/format";
import { BarList, ChartCard, LegendItem, StackedColumns, TrendLine, type BarItem } from "./charts";

const PERIODS = [7, 30, 90] as const;
const STATUS_ORDER: DocumentStatus[] = ["REVIEW_REQUIRED", "COMPLETED", "PROCESSING", "PENDING", "FAILED"];
const ACTIVITY_LABELS: Record<string, string> = {
  "document.uploaded": "uploaded",
  "document.version.uploaded": "uploaded a new version of",
  "document.processing.completed": "finished processing",
  "document.processing.failed": "could not process",
  "document.classification.corrected": "corrected the type of",
  "document.extraction.field_corrected": "corrected a value on",
  "review.requested": "requested a review of",
  "review_task.resolved": "resolved the review of",
  "workflow.started": "started a workflow on",
  "workflow.completed": "completed a workflow on",
  "workflow.rejected": "closed a rejected workflow on",
  "workflow.failed": "could not complete a workflow on",
  "workflow.action.approved": "approved a proposal on",
  "workflow.action.rejected": "rejected a proposal on",
};
const AUTO = "var(--viz-series-1)";
const REVIEWED = "var(--viz-series-2)";

// Keys come from the API as strings (and may be new to this build): fall back to the raw key.
function actionLabel(action: string): string {
  return (ACTION_LABELS as Record<string, string | undefined>)[action] ?? action;
}

function typeLabel(type: string): string {
  if (type === "UNCLASSIFIED") return "Unclassified";
  return (DOCUMENT_TYPE_LABELS as Record<string, string | undefined>)[type] ?? type;
}

function compact(value: number): string {
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function seconds(value: number | null): string {
  if (value === null) return "—";
  return value < 90 ? `${value.toFixed(1)} s` : `${(value / 60).toFixed(1)} min`;
}

function dayLabel(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

function StatTile({ label, value, detail, to }: { label: string; value: string; detail?: string; to?: string }) {
  const body = (
    <>
      <span className="block text-xs text-slate-500">{label}</span>
      <span className="mt-1 block text-2xl font-semibold text-slate-900">{value}</span>
      {detail && <span className="mt-0.5 block text-xs text-slate-500">{detail}</span>}
    </>
  );
  return (
    <li className="rounded-xl border border-slate-200 bg-white p-4">
      {to ? (
        <Link to={to} className="block hover:opacity-80">
          {body}
        </Link>
      ) : (
        body
      )}
    </li>
  );
}

function SimpleTable({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <table className="w-full text-left">
      <thead className="text-slate-500">
        <tr>
          {head.map((cell) => (
            <th key={cell} className="py-1 pr-3 font-medium">
              {cell}
            </th>
          ))}
        </tr>
      </thead>
      <tbody style={{ fontVariantNumeric: "tabular-nums" }}>
        {rows.map((row) => (
          <tr key={String(row[0])} className="border-t border-slate-100">
            {row.map((cell, index) => (
              <td key={`${String(row[0])}-${String(index)}`} className="py-1 pr-3">
                {cell}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function errorText(error: unknown): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : "The dashboard could not be loaded.";
}

export function DashboardPage() {
  const { token, user } = useAuth();
  const [days, setDays] = useState<(typeof PERIODS)[number]>(30);
  const summary = useQuery({
    queryKey: ["dashboard", days],
    queryFn: ({ signal }) =>
      apiRequest<DashboardSummary>(`/api/v1/dashboard/summary?days=${String(days)}`, { token, signal }),
    placeholderData: (previous) => previous,
    refetchInterval: 60_000,
  });
  const data = summary.data;
  const scope = user?.role === "ADMIN" ? "All departments" : (user?.department?.name ?? "Your documents");

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Dashboard</h1>
          <p className="mt-1 text-sm text-slate-600">{scope}: documents, checks, reviews and decisions.</p>
        </div>
        <label className="text-sm">
          <span className="mr-2 text-slate-600">Period</span>
          <select
            aria-label="Period"
            value={days}
            onChange={(event) => {
              setDays(Number(event.target.value) as (typeof PERIODS)[number]);
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            {PERIODS.map((period) => (
              <option key={period} value={period}>
                Last {period} days
              </option>
            ))}
          </select>
        </label>
      </div>

      {summary.isError && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {errorText(summary.error)}
        </p>
      )}
      {!data && summary.isPending && <p className="text-sm text-slate-500">Loading the dashboard…</p>}

      {data && (
        // Refetching keeps the previous render, dimmed: no layout jump.
        <div className={`space-y-6 transition-opacity ${summary.isPlaceholderData ? "opacity-60" : ""}`}>
          <ul aria-label="Key figures" className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <StatTile
              label="Documents"
              value={compact(data.documents.total)}
              detail={`${String(data.documents.uploaded_in_period)} uploaded in the period`}
              to="/documents"
            />
            <StatTile
              label="Open review tasks"
              value={compact(data.review_queue.open)}
              detail={`${String(data.review_queue.overdue)} overdue`}
              to="/reviews"
            />
            <StatTile
              label="Documents failing a rule"
              value={compact(data.discrepancies.documents_failing)}
              detail="standing now"
              to="/reviews"
            />
            <StatTile
              label="Awaiting approval"
              value={compact(data.workflows.awaiting_approval)}
              detail={`${String(data.workflows.finished_in_period)} workflows finished`}
              to="/workflows"
            />
            <StatTile
              label="Processing time"
              value={seconds(data.processing.average_seconds)}
              detail={`p95 ${seconds(data.processing.p95_seconds)} · ${String(data.processing.failed_in_period)} failed`}
            />
            <StatTile
              label={data.investigations.scope === "all" ? "AI investigations" : "Your AI investigations"}
              value={compact(data.investigations.in_period)}
              detail="in the period"
              to="/analysis"
            />
          </ul>

          <div className="grid gap-4 lg:grid-cols-2">
            <ChartCard
              title="Extraction confidence"
              caption="Daily mean of the current extractions' overall confidence (UTC days)"
              table={
                <SimpleTable
                  head={["Day", "Documents", "Mean confidence"]}
                  rows={data.confidence.map((point) => [
                    point.day,
                    point.processed,
                    point.extraction_confidence === null ? "—" : `${(point.extraction_confidence * 100).toFixed(1)}%`,
                  ])}
                />
              }
            >
              <TrendLine
                ariaLabel="Extraction confidence per day"
                max={1}
                format={(value) => `${Math.round(value * 100)}%`}
                points={data.confidence.map((point) => ({
                  label: dayLabel(point.day),
                  value: point.extraction_confidence,
                  detail: `${String(point.processed)} document(s)`,
                }))}
              />
            </ChartCard>

            <ChartCard
              title="Documents extracted per day"
              caption="Accepted automatically or sent to a reviewer"
              legend={
                <span className="flex gap-4">
                  <LegendItem color={AUTO} label="Accepted automatically" />
                  <LegendItem color={REVIEWED} label="Sent to review" />
                </span>
              }
              table={
                <SimpleTable
                  head={["Day", "Accepted automatically", "Sent to review"]}
                  rows={data.confidence.map((point) => [
                    point.day,
                    point.auto_accepted,
                    point.processed - point.auto_accepted,
                  ])}
                />
              }
            >
              <StackedColumns
                ariaLabel="Documents extracted per day"
                series={[
                  { label: "accepted automatically", color: AUTO },
                  { label: "sent to review", color: REVIEWED },
                ]}
                columns={data.confidence.map((point) => ({
                  label: dayLabel(point.day),
                  values: [point.auto_accepted, point.processed - point.auto_accepted],
                }))}
              />
            </ChartCard>

            <ChartCard
              title="Documents by status"
              table={
                <SimpleTable
                  head={["Status", "Documents"]}
                  rows={STATUS_ORDER.map((status) => [statusLabel(status), data.documents.by_status[status] ?? 0])}
                />
              }
            >
              <BarList
                ariaLabel="Documents by status"
                items={STATUS_ORDER.filter((status) => data.documents.by_status[status]).map(
                  (status): BarItem => ({
                    key: status,
                    label: statusLabel(status),
                    value: data.documents.by_status[status] ?? 0,
                    href: `/documents?status=${status}`,
                  }),
                )}
              />
            </ChartCard>

            <ChartCard
              title="Failing rules"
              caption="Documents failing each rule now (most common first)"
              table={
                <SimpleTable
                  head={["Rule", "Documents"]}
                  rows={data.discrepancies.by_rule.map((rule) => [rule.rule_code, rule.documents])}
                />
              }
            >
              <BarList
                ariaLabel="Failing rules"
                items={data.discrepancies.by_rule.map((rule) => ({
                  key: rule.rule_code,
                  label: rule.rule_code,
                  value: rule.documents,
                }))}
              />
            </ChartCard>

            <ChartCard
              title="Documents by type"
              table={
                <SimpleTable
                  head={["Type", "Documents"]}
                  rows={Object.entries(data.documents.by_type).map(([type, count]) => [type, count])}
                />
              }
            >
              <BarList
                ariaLabel="Documents by type"
                items={Object.entries(data.documents.by_type)
                  .sort((a, b) => b[1] - a[1])
                  .map(([type, count]) => ({
                    key: type,
                    label: typeLabel(type),
                    value: count,
                  }))}
              />
            </ChartCard>

            <ChartCard
              title="Decisions"
              caption={`Workflow outcomes and ${data.investigations.scope === "all" ? "investigation" : "your investigations'"} recommendations in the period`}
              table={
                <SimpleTable
                  head={["Kind", "Value", "Count"]}
                  rows={[
                    ...Object.entries(data.workflows.by_outcome).map(([outcome, count]) => [
                      `workflow: ${outcome}`,
                      outcomeLabel(outcome) ?? outcome,
                      count,
                    ]),
                    ...Object.entries(data.investigations.by_recommendation).map(([action, count]) => [
                      `investigation: ${action}`,
                      actionLabel(action),
                      count,
                    ]),
                  ]}
                />
              }
            >
              <p className="mb-2 text-xs font-medium text-slate-500">Workflow outcomes</p>
              <BarList
                ariaLabel="Workflow outcomes"
                items={Object.entries(data.workflows.by_outcome).map(([outcome, count]) => ({
                  key: outcome,
                  label: outcomeLabel(outcome) ?? outcome,
                  value: count,
                }))}
              />
              <p className="mb-2 mt-4 text-xs font-medium text-slate-500">Recommendations</p>
              <BarList
                ariaLabel="Investigation recommendations"
                items={Object.entries(data.investigations.by_recommendation).map(([action, count]) => ({
                  key: action,
                  label: actionLabel(action),
                  value: count,
                }))}
              />
            </ChartCard>
          </div>

          <section aria-labelledby="activity-heading" className="rounded-xl border border-slate-200 bg-white">
            <h2 id="activity-heading" className="border-b border-slate-200 px-5 py-3 font-medium">
              Recent activity
            </h2>
            {data.activity.length === 0 ? (
              <p className="px-5 py-4 text-sm text-slate-500">No activity on your documents yet.</p>
            ) : (
              <ul aria-label="Recent activity" className="divide-y divide-slate-100 text-sm">
                {data.activity.map((item) => (
                  <li key={item.id} className="flex flex-wrap items-baseline gap-x-2 px-5 py-2">
                    <time dateTime={item.occurred_at} className="w-36 shrink-0 text-xs text-slate-500">
                      {new Date(item.occurred_at).toLocaleString()}
                    </time>
                    <span className="font-medium text-slate-800">{item.actor}</span>
                    <span className="text-slate-600">{ACTIVITY_LABELS[item.action] ?? item.action}</span>
                    {item.document_id && (
                      <Link to={`/documents/${item.document_id}`} className="text-blue-900 hover:underline">
                        {item.document_name ?? "a document"}
                      </Link>
                    )}
                    {item.workflow_id && (
                      <Link to={`/workflows/${item.workflow_id}`} className="text-xs text-slate-500 hover:underline">
                        (workflow)
                      </Link>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
