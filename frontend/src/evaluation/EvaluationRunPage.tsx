import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { EvaluationDetail, EvaluationPage, EvaluationReportTable } from "../lib/types";
import { bound, formatDate, shortRevision } from "./format";
import { GateBadge } from "./GateBadge";

function ReportTableSection({ table, index }: { table: EvaluationReportTable; index: number }) {
  const headingId = `table-${String(index)}`;
  return (
    <section aria-labelledby={headingId} className="rounded-xl border border-slate-200 bg-white">
      <h2 id={headingId} className="px-5 pt-4 font-medium">
        {table.heading}
      </h2>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-xs text-slate-500">
            <tr>
              {table.header.map((cell, column) => (
                <th key={`${cell}-${String(column)}`} className="px-5 py-2 font-medium">
                  {cell}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100" style={{ fontVariantNumeric: "tabular-nums" }}>
            {table.rows.map((row, rowIndex) => (
              <tr key={String(rowIndex)}>
                {row.map((cell, column) => (
                  <td key={String(column)} className={`px-5 py-2 ${column === 0 ? "" : "whitespace-nowrap"}`}>
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function History({ suite, current }: { suite: string; current: string }) {
  const { token } = useAuth();
  const runs = useQuery({
    queryKey: ["evaluations", "history", suite],
    queryFn: () =>
      apiRequest<EvaluationPage>(`/api/v1/evaluations?suite=${suite}&include_quick=true&limit=20`, { token }),
  });
  if (!runs.data || runs.data.items.length < 2) return null;
  return (
    <section aria-labelledby="history-heading" className="rounded-xl border border-slate-200 bg-white p-5">
      <h2 id="history-heading" className="font-medium">
        Runs of this suite
      </h2>
      <ul className="mt-2 divide-y divide-slate-100 text-sm">
        {runs.data.items.map((run) => (
          <li key={run.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
            {run.id === current ? (
              <span className="font-medium">{formatDate(run.run_at)} (this run)</span>
            ) : (
              <Link to={`/evaluation/${run.id}`} className="text-blue-900 hover:underline">
                {formatDate(run.run_at)}
              </Link>
            )}
            <span className="font-mono text-xs text-slate-500">{shortRevision(run.git_revision)}</span>
            {run.quick && <span className="rounded bg-amber-50 px-2 py-0.5 text-xs text-amber-800">quick</span>}
            <GateBadge gates={run.gates} />
          </li>
        ))}
      </ul>
    </section>
  );
}

export function EvaluationRunPage() {
  const { evaluationId = "" } = useParams();
  const { token } = useAuth();
  const run = useQuery({
    queryKey: ["evaluations", evaluationId],
    queryFn: () => apiRequest<EvaluationDetail>(`/api/v1/evaluations/${evaluationId}`, { token }),
  });
  if (run.isPending) return <p className="text-sm text-slate-500">Loading…</p>;
  if (run.isError) {
    return (
      <p role="alert" className="text-sm text-red-700">
        {run.error instanceof ApiError
          ? (run.error.problem?.detail ?? run.error.message)
          : "The evaluation could not be loaded."}
      </p>
    );
  }
  const data = run.data;
  return (
    <div className="space-y-6">
      <div>
        <Link to="/evaluation" className="text-sm text-blue-900 hover:underline">
          ← Evaluation
        </Link>
        <div className="mt-1 flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold">{data.title}</h1>
          <GateBadge gates={data.gates} />
          {data.quick && (
            <span className="rounded bg-amber-50 px-2 py-0.5 text-xs text-amber-800">
              quick run: small datasets, not comparable with full runs
            </span>
          )}
        </div>
        <p className="mt-1 text-sm text-slate-600">
          <span className="font-mono">{data.suite}</span> · commit{" "}
          <span className="font-mono">{shortRevision(data.git_revision)}</span> · run {formatDate(data.run_at)} ·{" "}
          {data.source === "RUN" ? "recorded by the run" : "imported from the report files"}
          {data.recorded_by ? ` (${data.recorded_by})` : ""} on {formatDate(data.recorded_at)}
        </p>
      </div>

      {data.gate_checks.length > 0 && (
        <section aria-labelledby="gates-heading" className="rounded-xl border border-slate-200 bg-white">
          <h2 id="gates-heading" className="px-5 pt-4 font-medium">
            Regression gates
          </h2>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 text-xs text-slate-500">
                <tr>
                  <th className="px-5 py-2 font-medium">Metric</th>
                  <th className="py-2 font-medium">Value</th>
                  <th className="py-2 font-medium">Bound</th>
                  <th className="py-2 font-medium">Result</th>
                  <th className="py-2 pr-5 font-medium">Why</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.gate_checks.map((check) => (
                  <tr key={check.metric.join("/")} className="align-top">
                    <td className="px-5 py-2 font-mono text-xs">{check.metric.join(" / ")}</td>
                    <td className="py-2">{check.value ?? "missing"}</td>
                    <td className="whitespace-nowrap py-2">{bound(check)}</td>
                    <td className="py-2">
                      {check.passed ? (
                        <span className="text-emerald-800">Passed</span>
                      ) : (
                        <span className="font-medium text-red-800">Broken: {check.problem}</span>
                      )}
                    </td>
                    <td className="py-2 pr-5 text-slate-600">{check.why}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {data.tables.length === 0 ? (
        <p className="text-sm text-slate-600">
          This report predates tables in the JSON report; download the Markdown report from the repository.
        </p>
      ) : (
        data.tables.map((table, index) => <ReportTableSection key={table.heading} table={table} index={index} />)
      )}

      {data.notes.length > 0 && (
        <section aria-labelledby="notes-heading" className="rounded-xl border border-slate-200 bg-white p-5">
          <h2 id="notes-heading" className="font-medium">
            Notes
          </h2>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-700">
            {data.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </section>
      )}

      <details className="rounded-xl border border-slate-200 bg-white p-5 text-sm">
        <summary className="cursor-pointer font-medium">Datasets, configuration and environment</summary>
        <pre
          aria-label="Provenance"
          className="mt-3 overflow-x-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-slate-700"
        >
          {JSON.stringify({ dataset: data.dataset, config: data.config, environment: data.environment }, null, 2)}
        </pre>
      </details>

      <History suite={data.suite} current={data.id} />
    </div>
  );
}
