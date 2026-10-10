import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { useAuth } from "../auth/useAuth";
import { DOCUMENT_TYPE_LABELS } from "../documents/format";
import { formatPeriod } from "../knowledge/format";
import { GenerateReportButton } from "../reports/GenerateReportButton";
import { ApiError, apiRequest } from "../lib/api";
import type { AnalysisEvidence, AnalysisResult, AnalysisRun, AnalysisSource } from "../lib/types";
import {
  ACTION_LABELS,
  ACTIVE,
  CATEGORY_LABELS,
  CATEGORY_ORDER,
  CATEGORY_STYLES,
  CONFIDENCE_STYLES,
  INTENT_LABELS,
  NODE_LABELS,
  STATUS_STYLES,
} from "./format";

function EvidenceChip({ label, evidence }: { label: string; evidence: AnalysisEvidence | undefined }) {
  const target = label.startsWith("K") ? `#source-${label}` : undefined;
  const chip = (
    <span
      title={evidence?.text ?? "unknown evidence"}
      className="ml-1 rounded bg-slate-100 px-1 font-mono text-xs text-slate-700"
    >
      {label}
    </span>
  );
  return target ? (
    <a href={target} aria-label={`Source ${label}`}>
      {chip}
    </a>
  ) : (
    chip
  );
}

export function Findings({ result }: { result: AnalysisResult }) {
  const evidence = new Map(result.evidence.map((item) => [item.label, item]));
  const findings = [...result.findings].sort(
    (a, b) => CATEGORY_ORDER.indexOf(a.category) - CATEGORY_ORDER.indexOf(b.category),
  );
  return (
    <section aria-label="Findings">
      <h2 className="text-sm font-semibold text-slate-700">Findings</h2>
      <ul className="mt-2 space-y-2">
        {findings.map((finding, index) => (
          <li key={index} className="flex items-start gap-2 text-sm">
            <span className={`shrink-0 rounded px-1.5 py-0.5 text-xs ${CATEGORY_STYLES[finding.category]}`}>
              {CATEGORY_LABELS[finding.category]}
            </span>
            <span className={finding.grounded ? "" : "bg-amber-50"}>
              {finding.statement}
              {finding.evidence.map((label) => (
                <EvidenceChip key={label} label={label} evidence={evidence.get(label)} />
              ))}
              {finding.source === "model" && <span className="ml-2 text-xs text-amber-800">AI-written, checked</span>}
              {!finding.grounded && <span className="ml-2 text-xs text-amber-800">check against the source</span>}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function SourceItem({ source }: { source: AnalysisSource }) {
  const [open, setOpen] = useState(false);
  const period = formatPeriod(source.effective_from, source.effective_to);
  return (
    <li id={`source-${source.label ?? source.chunk_id}`} className="rounded-lg border border-slate-200 bg-white p-3 text-sm">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="rounded bg-violet-900 px-1.5 py-0.5 font-mono text-xs text-white">{source.label}</span>
        <Link to={`/knowledge/${source.knowledge_document_id}`} className="font-medium text-blue-900 hover:underline">
          {source.title}
        </Link>
        {source.version_label && <span className="text-slate-500">v{source.version_label}</span>}
        <span className="text-xs text-slate-500">
          {[source.section_path, period && `in force ${period}`].filter(Boolean).join(" · ")}
        </span>
      </div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen(!open);
        }}
        className="mt-1 text-xs text-blue-900 hover:underline"
      >
        {open ? "Hide passage" : "Show passage"}
      </button>
      {open && <p className="mt-2 whitespace-pre-wrap text-slate-800">{source.content}</p>}
    </li>
  );
}

function Recommendation({ result, allowActions }: { result: AnalysisResult; allowActions: boolean }) {
  const recommendation = result.recommendation;
  const action = result.action;
  return (
    <section aria-label="Recommendation" className="rounded-xl border border-slate-200 bg-white p-5">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-semibold">{ACTION_LABELS[recommendation.action]}</h2>
        <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-700">risk {recommendation.risk.toLowerCase()}</span>
        {recommendation.requires_approval && (
          <span className="rounded bg-amber-50 px-2 py-0.5 text-xs text-amber-800">
            needs approval by a {recommendation.required_role?.toLowerCase() ?? "manager"}
          </span>
        )}
        <span className="text-xs text-slate-500">
          {recommendation.source === "model" ? "proposed by the AI model, allowed by the guardrails" : "decided by the rules"}
        </span>
      </div>
      <p className="mt-2 text-sm text-slate-800">{recommendation.rationale}</p>
      {recommendation.guardrail_notes.map((note) => (
        <p key={note} className="mt-1 text-xs text-slate-600">
          Guardrail: {note}
        </p>
      ))}
      {action && (
        <p className="mt-3 text-sm" aria-label="Action">
          <span className="font-medium">
            {action.status === "EXECUTED"
              ? "Done: "
              : action.status === "PROPOSED"
                ? "Proposed, not executed: "
                : action.status === "SKIPPED"
                  ? "Not done: "
                  : "Failed: "}
          </span>
          {action.detail}
          {action.review_task_id && (
            <>
              {" "}
              <Link to="/reviews" className="text-blue-900 underline">
                Open the review queue
              </Link>
            </>
          )}
        </p>
      )}
      {!action && !allowActions && recommendation.action === "HOLD_FOR_REVIEW" && (
        <p className="mt-3 text-sm text-slate-600">Safe actions were turned off for this investigation.</p>
      )}
    </section>
  );
}

export function AnalysisRunPage() {
  const { runId } = useParams();
  const { token } = useAuth();
  const run = useQuery({
    queryKey: ["analysis", runId],
    queryFn: () => apiRequest<AnalysisRun>(`/api/v1/analysis/${runId ?? ""}`, { token }),
    refetchInterval: (query) => (query.state.data && ACTIVE.includes(query.state.data.status) ? 1500 : false),
  });
  if (run.error) {
    return (
      <p role="alert" className="text-sm text-red-700">
        {run.error instanceof ApiError ? (run.error.problem?.detail ?? run.error.message) : "Loading failed."}
      </p>
    );
  }
  if (!run.data) return <p className="text-sm text-slate-500">Loading…</p>;
  const data = run.data;
  const result = data.result;
  return (
    <div className="space-y-6">
      <div>
        <Link to="/analysis" className="text-sm text-blue-900 hover:underline">
          ← AI analysis
        </Link>
        <h1 className="mt-1 text-xl font-semibold">{data.query}</h1>
        <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
          <span className={`rounded px-2 py-0.5 text-xs ${STATUS_STYLES[data.status]}`}>{data.status}</span>
          {data.plan && <span className="text-slate-600">{INTENT_LABELS[data.plan.intent]}</span>}
          {result && (
            <span className={`rounded px-2 py-0.5 text-xs ${CONFIDENCE_STYLES[result.confidence.level]}`}>
              confidence {result.confidence.level.toLowerCase()} ({Math.round(result.confidence.score * 100)}%)
            </span>
          )}
          {result && result.documents.length > 0 && <GenerateReportButton reportType="AI_ANALYSIS" subjectId={data.id} />}
        </div>
      </div>
      {ACTIVE.includes(data.status) && (
        <p className="text-sm text-slate-600" role="status">
          The investigation is {data.status === "QUEUED" ? "queued" : "running"}…
        </p>
      )}
      {data.status === "FAILED" && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {data.error ?? "The investigation failed."}
        </p>
      )}
      {result && (
        <>
          <section aria-label="Summary" className="space-y-1">
            <p className="text-base text-slate-900">{result.summary}</p>
            <p className="text-xs text-slate-500">
              {result.summary_source === "model" && result.model
                ? `Summary written by ${result.model.model} and checked against the evidence`
                : "Summary from the rules (no AI model involved)"}
            </p>
          </section>
          <Recommendation result={result} allowActions={data.allow_safe_actions} />
          {result.documents.length > 0 && (
            <section aria-label="Documents">
              <h2 className="text-sm font-semibold text-slate-700">Documents</h2>
              <ul className="mt-2 space-y-1 text-sm">
                {result.documents.map((document) => (
                  <li key={document.document_id}>
                    <span className="mr-2 rounded bg-slate-100 px-1 font-mono text-xs">{document.label}</span>
                    <Link to={`/documents/${document.document_id}`} className="text-blue-900 hover:underline">
                      {document.filename}
                    </Link>
                    <span className="ml-2 text-slate-600">
                      {[
                        document.document_type ? DOCUMENT_TYPE_LABELS[document.document_type] : null,
                        document.vendor_name,
                        document.total && `${document.total} ${document.currency ?? ""}`.trim(),
                        document.role === "related" ? "related" : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          <Findings result={result} />
          {result.sources.length > 0 && (
            <section aria-label="Sources">
              <h2 className="text-sm font-semibold text-slate-700">Policy sources</h2>
              <ol className="mt-2 space-y-2">
                {result.sources.map((source) => (
                  <SourceItem key={source.chunk_id} source={source} />
                ))}
              </ol>
            </section>
          )}
          <section aria-label="Confidence">
            <h2 className="text-sm font-semibold text-slate-700">Why this confidence</h2>
            {result.confidence.factors.length === 0 ? (
              <p className="mt-1 text-sm text-slate-600">No weakness found in the evidence.</p>
            ) : (
              <ul className="mt-1 space-y-1 text-sm text-slate-700">
                {result.confidence.factors.map((factor) => (
                  <li key={factor.factor}>
                    {factor.detail} <span className="text-xs text-slate-500">({factor.effect})</span>
                  </li>
                ))}
              </ul>
            )}
          </section>
          {result.notices.length > 0 && (
            <ul aria-label="Notices" className="space-y-1 text-sm text-slate-600">
              {result.notices.map((notice) => (
                <li key={notice}>• {notice}</li>
              ))}
            </ul>
          )}
        </>
      )}
      {data.tool_call_log.length > 0 && (
        <details className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
          <summary className="cursor-pointer font-medium text-slate-700">
            Steps and tool calls ({data.usage.tool_calls} tool calls, {data.usage.llm_calls} model calls)
          </summary>
          <ol className="mt-2 space-y-1 text-xs text-slate-600" aria-label="Steps">
            {data.trace.map((step, index) => (
              <li key={index}>
                {NODE_LABELS[step.node] ?? step.node} · {Math.round(step.duration_ms)} ms
                {step.tool_calls > 0 && ` · ${String(step.tool_calls)} tool call(s)`}
              </li>
            ))}
          </ol>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="text-slate-500">
                <tr>
                  <th className="py-1 font-medium">Tool</th>
                  <th className="font-medium">Step</th>
                  <th className="font-medium">Outcome</th>
                  <th className="font-medium">Time</th>
                </tr>
              </thead>
              <tbody>
                {data.tool_call_log.map((call) => (
                  <tr key={call.id}>
                    <td className="py-1 font-mono">{call.tool_name}</td>
                    <td>{call.node_name ? (NODE_LABELS[call.node_name] ?? call.node_name) : "—"}</td>
                    <td className={call.status === "SUCCEEDED" ? "text-emerald-700" : "text-red-700"}>
                      {call.status}
                      {call.error && ` — ${call.error}`}
                    </td>
                    <td>{Math.round(Number(call.latency_ms))} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}
