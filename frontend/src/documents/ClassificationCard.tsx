import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { Classification, DocumentDetail, DocumentType } from "../lib/types";
import { DOCUMENT_TYPE_LABELS, DOCUMENT_TYPES, formatDateTime, formatPercent, METHOD_LABELS } from "./format";

function Evidence({ classification }: { classification: Classification }) {
  const { local = [], keywords = {}, llm, external_ai: gate } = classification.signals;
  const cues = Object.entries(keywords).filter(([, found]) => found.length > 0);
  return (
    <dl className="mt-4 grid gap-4 text-sm md:grid-cols-3">
      <div>
        <dt className="text-xs text-slate-500">Local model</dt>
        <dd className="mt-1 space-y-1">
          {local.length === 0 && <span className="text-slate-500">—</span>}
          {local.map((entry) => (
            <div key={entry.label} className="flex items-center gap-2">
              <span className="w-28 truncate">{DOCUMENT_TYPE_LABELS[entry.label]}</span>
              <span className="h-1.5 flex-1 rounded bg-slate-100">
                <span
                  className="block h-1.5 rounded bg-blue-900"
                  style={{ width: `${Math.round(entry.probability * 100)}%` }}
                />
              </span>
              <span className="w-10 text-right text-xs tabular-nums">{formatPercent(entry.probability)}</span>
            </div>
          ))}
        </dd>
      </div>
      <div>
        <dt className="text-xs text-slate-500">Keyword evidence</dt>
        <dd className="mt-1 space-y-1">
          {cues.length === 0 && <span className="text-slate-500">none</span>}
          {cues.map(([label, found]) => (
            <div key={label}>
              <span className="font-medium">{DOCUMENT_TYPE_LABELS[label as DocumentType]}:</span>{" "}
              <span className="text-slate-600">{found.join(", ")}</span>
            </div>
          ))}
        </dd>
      </div>
      <div>
        <dt className="text-xs text-slate-500">External AI</dt>
        <dd className="mt-1 text-slate-700">
          {llm?.used
            ? `Consulted (${llm.model ?? "LLM"}): ${llm.label ? DOCUMENT_TYPE_LABELS[llm.label] : "—"}, quote ${
                llm.quote_found ? "found in the text" : "NOT found in the text"
              }`
            : llm
              ? `Not used: ${llm.reason ?? "—"}`
              : "Not needed (local model was confident)"}
          {gate && !gate.allowed && (
            <span className="mt-1 block text-xs text-amber-800">
              Effective sensitivity {gate.effective_sensitivity.toLowerCase()}
            </span>
          )}
        </dd>
      </div>
    </dl>
  );
}

export function ClassificationCard({ document }: { document: DocumentDetail }) {
  const { token, user } = useAuth();
  const queryClient = useQueryClient();
  const current = document.classification;
  const canReview = user?.permissions.includes("documents:review") ?? false;
  const [label, setLabel] = useState<DocumentType>(current?.label ?? "OTHER");
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const correct = useMutation({
    mutationFn: () =>
      apiRequest<Classification>(`/api/v1/documents/${document.id}/classification`, {
        method: "PATCH",
        token,
        body: { document_type: label, note: note.trim() || null },
      }),
    onSuccess: async () => {
      setEditing(false);
      setNote("");
      setError(null);
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
    onError: (failure) => {
      setError(failure instanceof ApiError ? (failure.problem?.detail ?? failure.message) : "The request failed.");
    },
  });

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    correct.mutate();
  };

  const history = document.classification_history.filter((entry) => !entry.is_current);

  return (
    <section aria-labelledby="classification-heading" className="rounded-xl border border-slate-200 bg-white p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="classification-heading" className="font-medium">
            Document type
          </h2>
          {current ? (
            <p className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="text-lg font-semibold">{DOCUMENT_TYPE_LABELS[current.label]}</span>
              <span className="text-sm tabular-nums text-slate-600">{formatPercent(current.confidence)} confidence</span>
              <span className="text-xs text-slate-500">
                {METHOD_LABELS[current.method]}
                {current.created_by && ` · ${current.created_by.full_name}`} · {formatDateTime(current.created_at)}
              </span>
            </p>
          ) : (
            <p className="mt-2 text-sm text-slate-500">Not classified yet.</p>
          )}
          {current?.note && <p className="mt-1 text-sm text-slate-600">Note: {current.note}</p>}
        </div>
        {canReview && !editing && (
          <button
            type="button"
            onClick={() => {
              setEditing(true);
            }}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100"
          >
            Correct type
          </button>
        )}
      </div>

      {editing && (
        <form aria-label="Correct document type" onSubmit={submit} className="mt-4 flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <span className="block text-xs text-slate-500">Document type</span>
            <select
              value={label}
              onChange={(event) => {
                setLabel(event.target.value as DocumentType);
              }}
              className="mt-1 rounded-md border border-slate-300 px-3 py-1.5"
            >
              {DOCUMENT_TYPES.map((value) => (
                <option key={value} value={value}>
                  {DOCUMENT_TYPE_LABELS[value]}
                </option>
              ))}
            </select>
          </label>
          <label className="min-w-64 flex-1 text-sm">
            <span className="block text-xs text-slate-500">Note (optional)</span>
            <input
              value={note}
              maxLength={500}
              onChange={(event) => {
                setNote(event.target.value);
              }}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5"
            />
          </label>
          <button
            type="submit"
            disabled={correct.isPending}
            className="rounded-md bg-blue-900 px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            Save
          </button>
          <button
            type="button"
            onClick={() => {
              setEditing(false);
            }}
            className="rounded-md px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100"
          >
            Cancel
          </button>
        </form>
      )}
      {error && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}

      {current && current.method !== "HUMAN" && <Evidence classification={current} />}

      {history.length > 0 && (
        <details className="mt-4 text-sm">
          <summary className="cursor-pointer text-slate-600">Earlier classifications ({history.length})</summary>
          <ul className="mt-2 space-y-1 text-slate-600">
            {history.map((entry) => (
              <li key={entry.id}>
                {DOCUMENT_TYPE_LABELS[entry.label]} · {formatPercent(entry.confidence)} · {METHOD_LABELS[entry.method]} ·{" "}
                {formatDateTime(entry.created_at)}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
