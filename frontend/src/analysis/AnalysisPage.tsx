import { useMutation, useQuery } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { AnalysisPage as AnalysisList, AnalysisRun, DocumentDetail } from "../lib/types";
import { ACTION_LABELS, ACTIVE, CONFIDENCE_STYLES, INTENT_LABELS, STATUS_STYLES } from "./format";

const EXAMPLES = [
  "Can we pay this invoice?",
  "Why does this invoice not match its purchase order?",
  "Is this invoice a duplicate?",
  "Who must approve payment terms longer than 60 days?",
];

function errorText(error: unknown): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : "The request failed.";
}

function NewInvestigation() {
  const { token } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [question, setQuestion] = useState("");
  const [documentIds, setDocumentIds] = useState<string[]>(params.getAll("document"));
  const [allowActions, setAllowActions] = useState(true);
  const named = useQuery({
    queryKey: ["analysis-documents", documentIds],
    queryFn: () =>
      Promise.all(
        documentIds.map((id) => apiRequest<DocumentDetail>(`/api/v1/documents/${id}`, { token })),
      ),
    enabled: documentIds.length > 0,
  });
  const start = useMutation({
    mutationFn: () =>
      apiRequest<AnalysisRun>("/api/v1/analysis", {
        method: "POST",
        token,
        body: { query: question.trim(), document_ids: documentIds, allow_safe_actions: allowActions },
      }),
    onSuccess: (run) => {
      void navigate(`/analysis/${run.id}`);
    },
  });
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (question.trim().length >= 3) start.mutate();
  };
  return (
    <form onSubmit={submit} aria-label="New investigation" className="space-y-3 rounded-xl border border-slate-200 bg-white p-5">
      <label htmlFor="analysis-question" className="block text-sm font-medium text-slate-700">
        What should be investigated?
      </label>
      <textarea
        id="analysis-question"
        value={question}
        maxLength={1000}
        rows={2}
        placeholder="e.g. Can we pay invoice INV-2026-0042 from Kestrel Industrial Supply?"
        onChange={(event) => {
          setQuestion(event.target.value);
        }}
        className="block w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
      />
      <div className="flex flex-wrap gap-2 text-xs">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => {
              setQuestion(example);
            }}
            className="rounded-full border border-slate-300 px-3 py-1 text-slate-700 hover:bg-slate-100"
          >
            {example}
          </button>
        ))}
      </div>
      {documentIds.length > 0 && (
        <div className="text-sm" aria-label="Documents to investigate">
          <span className="text-slate-500">Documents: </span>
          {documentIds.map((id, index) => (
            <span key={id} className="mr-2 inline-flex items-center gap-1 rounded bg-slate-100 px-2 py-0.5">
              {named.data?.[index]?.display_filename ?? id.slice(0, 8)}
              <button
                type="button"
                aria-label={`Remove ${named.data?.[index]?.display_filename ?? id}`}
                onClick={() => {
                  setDocumentIds(documentIds.filter((other) => other !== id));
                }}
                className="text-slate-500 hover:text-slate-900"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <label className="flex items-center gap-2 text-sm text-slate-700">
        <input
          type="checkbox"
          checked={allowActions}
          onChange={(event) => {
            setAllowActions(event.target.checked);
          }}
        />
        Let the investigation request a human review itself (other actions are only proposed)
      </label>
      {start.error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {errorText(start.error)}
        </p>
      )}
      <button
        type="submit"
        disabled={question.trim().length < 3 || start.isPending}
        className="rounded-md bg-blue-900 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800 disabled:opacity-50"
      >
        {start.isPending ? "Starting…" : "Investigate"}
      </button>
    </form>
  );
}

export function AnalysisPage() {
  const { token, user } = useAuth();
  const canRun = user?.permissions.includes("analysis:run") ?? false;
  const runs = useQuery({
    queryKey: ["analyses"],
    queryFn: () => apiRequest<AnalysisList>("/api/v1/analysis?limit=25", { token }),
    refetchInterval: (query) =>
      query.state.data?.items.some((run) => ACTIVE.includes(run.status)) ? 2000 : false,
  });
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">AI analysis</h1>
        <p className="mt-1 text-sm text-slate-600">
          The investigation agent gathers facts with the same permissions as you — documents, extracted fields and
          their evidence, business rules, comparisons and policies — and recommends one action. Facts come from the
          tools; anything an AI model adds is checked against them and labelled.
        </p>
      </div>
      {canRun && <NewInvestigation />}
      <section aria-label="Investigations">
        <h2 className="text-sm font-semibold text-slate-700">Your investigations</h2>
        {runs.error && (
          <p role="alert" className="mt-2 text-sm text-red-700">
            {errorText(runs.error)}
          </p>
        )}
        {runs.data && runs.data.items.length === 0 && (
          <p className="mt-2 text-sm text-slate-500">No investigations yet.</p>
        )}
        {runs.data && runs.data.items.length > 0 && (
          <table className="mt-2 w-full text-left text-sm">
            <thead className="text-xs text-slate-500">
              <tr>
                <th className="py-2 font-medium">Question</th>
                <th className="font-medium">Kind</th>
                <th className="font-medium">Status</th>
                <th className="font-medium">Recommendation</th>
                <th className="font-medium">Confidence</th>
                <th className="font-medium">Started</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 bg-white">
              {runs.data.items.map((run) => (
                <tr key={run.id}>
                  <td className="max-w-md py-2 pr-3">
                    <Link to={`/analysis/${run.id}`} className="text-blue-900 hover:underline">
                      {run.query}
                    </Link>
                  </td>
                  <td className="pr-3 text-slate-600">{run.intent ? INTENT_LABELS[run.intent] : "—"}</td>
                  <td className="pr-3">
                    <span className={`rounded px-2 py-0.5 text-xs ${STATUS_STYLES[run.status]}`}>{run.status}</span>
                  </td>
                  <td className="pr-3">{run.recommendation ? ACTION_LABELS[run.recommendation] : "—"}</td>
                  <td className="pr-3">
                    {run.confidence ? (
                      <span className={`rounded px-2 py-0.5 text-xs ${CONFIDENCE_STYLES[run.confidence]}`}>
                        {run.confidence}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="text-xs text-slate-500">{new Date(run.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
