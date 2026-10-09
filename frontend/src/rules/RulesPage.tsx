import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";

import { useAuth } from "../auth/useAuth";
import { DOCUMENT_TYPE_LABELS, formatDateTime } from "../documents/format";
import { ApiError, apiRequest } from "../lib/api";
import type { Rule, Severity } from "../lib/types";
import { SEVERITY_LABELS } from "../review/format";

const SEVERITIES: Severity[] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

function RuleEditor({ rule, onDone }: { rule: Rule; onDone: () => void }) {
  const { token } = useAuth();
  const queryClient = useQueryClient();
  const [params, setParams] = useState(JSON.stringify(rule.params, null, 2));
  const [severity, setSeverity] = useState<Severity>(rule.severity);
  const [enabled, setEnabled] = useState(rule.is_enabled);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      apiRequest<Rule>(`/api/v1/rules/${encodeURIComponent(rule.code)}`, { method: "PATCH", token, body }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["rules"] });
      onDone();
    },
    onError: (failure) => {
      setError(failure instanceof ApiError ? (failure.problem?.detail ?? failure.message) : "The request failed.");
    },
  });
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    let parsed: unknown;
    try {
      parsed = JSON.parse(params);
    } catch {
      setError("Parameters must be valid JSON.");
      return;
    }
    save.mutate({ params: parsed, severity, is_enabled: enabled, note: note.trim() || null });
  };
  return (
    <form aria-label={`Edit ${rule.code}`} onSubmit={submit} className="mt-3 space-y-3 rounded-lg bg-slate-50 p-3">
      <label className="block text-xs">
        <span className="text-slate-500">Parameters (JSON)</span>
        <textarea
          value={params}
          rows={Math.min(12, params.split("\n").length + 1)}
          onChange={(event) => {
            setParams(event.target.value);
          }}
          className="mt-1 block w-full rounded-md border border-slate-300 font-mono text-xs"
        />
      </label>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs">
          <span className="block text-slate-500">Severity</span>
          <select
            value={severity}
            onChange={(event) => {
              setSeverity(event.target.value as Severity);
            }}
            className="mt-1 rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            {SEVERITIES.map((value) => (
              <option key={value} value={value}>
                {SEVERITY_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => {
              setEnabled(event.target.checked);
            }}
          />
          Enabled
        </label>
        <label className="text-xs">
          <span className="block text-slate-500">Reason for the change</span>
          <input
            value={note}
            maxLength={500}
            onChange={(event) => {
              setNote(event.target.value);
            }}
            className="mt-1 rounded-md border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <button
          type="submit"
          disabled={save.isPending}
          className="rounded-md bg-blue-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          Save
        </button>
        <button type="button" onClick={onDone} className="rounded-md px-2 py-1.5 text-sm text-slate-600 hover:bg-slate-100">
          Cancel
        </button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      )}
    </form>
  );
}

export function RulesPage() {
  const { token, user } = useAuth();
  const canManage = user?.permissions.includes("rules:manage") ?? false;
  const [editing, setEditing] = useState<string | null>(null);
  const rules = useQuery({
    queryKey: ["rules"],
    queryFn: ({ signal }) => apiRequest<Rule[]>("/api/v1/rules", { token, signal }),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Business rules</h1>
        <p className="mt-1 text-sm text-slate-500">
          Deterministic checks run on every processed document and on its comparison with the order and deliveries.
          Changes apply to new evaluations; documents already processed keep their results until re-evaluated.
        </p>
      </div>
      {rules.isPending ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : rules.isError ? (
        <p role="alert" className="text-sm text-red-700">
          The rules could not be loaded.
        </p>
      ) : (
        <ul className="space-y-3">
          {rules.data.map((rule) => (
            <li
              key={rule.code}
              aria-label={rule.code}
              className={`rounded-xl border border-slate-200 bg-white p-4 ${rule.is_enabled ? "" : "opacity-70"}`}
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="font-medium">
                    {rule.name}
                    {!rule.is_enabled && <span className="ml-2 text-xs font-normal text-slate-500">(disabled)</span>}
                  </p>
                  <p className="text-xs text-slate-500">
                    <span className="font-mono">{rule.code}</span> · {SEVERITY_LABELS[rule.severity]} ·{" "}
                    {rule.applies_to.map((type) => DOCUMENT_TYPE_LABELS[type]).join(", ")} · version {rule.version}
                    {rule.updated_by && ` · changed by ${rule.updated_by.full_name} ${formatDateTime(rule.updated_at)}`}
                  </p>
                  <p className="mt-1 text-sm text-slate-700">{rule.description}</p>
                  {Object.keys(rule.params).length > 0 && (
                    <p className="mt-1 font-mono text-xs text-slate-500">{JSON.stringify(rule.params)}</p>
                  )}
                </div>
                {canManage && editing !== rule.code && (
                  <button
                    type="button"
                    onClick={() => {
                      setEditing(rule.code);
                    }}
                    className="rounded-md border border-slate-300 px-3 py-1 text-sm hover:bg-slate-100"
                  >
                    Edit
                  </button>
                )}
              </div>
              {editing === rule.code && (
                <RuleEditor
                  rule={rule}
                  onDone={() => {
                    setEditing(null);
                  }}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
