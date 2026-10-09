import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type SubmitEvent, useEffect, useRef, useState } from "react";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { ExtractedField, Extraction, Highlight } from "../lib/types";
import {
  EVIDENCE_LABELS,
  EXTRACTION_METHOD_LABELS,
  fieldLabel,
  fieldValue,
  formatPercent,
  REVIEW_LEVEL_LABELS,
  REVIEW_LEVEL_STYLES,
} from "./format";

// Display hint only: values below this are marked for attention (the server decides routing).
const ATTENTION_BELOW = 0.85;

function evidenceStyle(field: ExtractedField): string {
  switch (field.evidence_status) {
    case "VERIFIED":
    case "HUMAN":
      return "text-emerald-700";
    case "FUZZY":
      return "text-amber-700";
    default:
      return "text-red-700";
  }
}

function CorrectionForm({
  documentId,
  field,
  onDone,
}: {
  documentId: string;
  field: ExtractedField;
  onDone: () => void;
}) {
  const { token } = useAuth();
  const queryClient = useQueryClient();
  const [value, setValue] = useState(field.corrected_value ?? field.original_value ?? "");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () =>
      apiRequest<ExtractedField>(`/api/v1/documents/${documentId}/extraction/fields/${field.id}`, {
        method: "PATCH",
        token,
        body: { value, note: note.trim() || null },
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
      onDone();
    },
    onError: (failure) => {
      setError(failure instanceof ApiError ? (failure.problem?.detail ?? failure.message) : "The request failed.");
    },
  });
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    save.mutate();
  };
  return (
    <form
      aria-label={`Correct ${fieldLabel(field.field_name)}`}
      onSubmit={submit}
      className="mt-2 flex flex-wrap items-end gap-2"
    >
      <label className="text-xs">
        <span className="block text-slate-500">Value as printed (empty = not on the document)</span>
        <input
          value={value}
          maxLength={500}
          onChange={(event) => {
            setValue(event.target.value);
          }}
          className="mt-1 rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
      </label>
      <label className="text-xs">
        <span className="block text-slate-500">Note (optional)</span>
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
        className="rounded-md bg-blue-900 px-3 py-1 text-sm font-medium text-white disabled:opacity-50"
      >
        Save
      </button>
      <button type="button" onClick={onDone} className="rounded-md px-2 py-1 text-sm text-slate-600 hover:bg-slate-100">
        Cancel
      </button>
      {error && (
        <p role="alert" className="w-full text-sm text-red-700">
          {error}
        </p>
      )}
    </form>
  );
}

function FieldRow({
  documentId,
  field,
  canReview,
  focused,
  onShow,
}: {
  documentId: string;
  field: ExtractedField;
  canReview: boolean;
  focused: boolean;
  onShow: (highlight: Highlight) => void;
}) {
  const [editing, setEditing] = useState(false);
  const confidence = Number(field.confidence);
  const found = field.original_value !== null || field.corrected_value !== null;
  const printed = field.original_value;
  const shown = fieldValue(field);
  return (
    <tr
      aria-current={focused || undefined}
      className={focused ? "bg-blue-50 ring-2 ring-blue-300" : found && confidence < ATTENTION_BELOW ? "bg-amber-50/60" : undefined}
    >
      <td className="py-2 pr-3 align-top">
        {fieldLabel(field.field_name)}
        {field.is_required && (
          <>
            {" "}
            <span className="text-xs text-slate-400">required</span>
          </>
        )}
      </td>
      <td className="py-2 pr-3 align-top">
        {found ? (
          <>
            <span className="font-medium">{shown}</span>
            {printed !== null && field.corrected_value === null && printed !== shown && (
              <span className="block text-xs text-slate-500">printed: {printed}</span>
            )}
            {field.corrected_value !== null && (
              <span className="block text-xs text-slate-500">
                corrected by {field.corrected_by?.full_name ?? "a reviewer"}
                {printed !== null && ` · printed: ${printed}`}
              </span>
            )}
            {field.normalized_value?.status === "UNCERTAIN" && field.corrected_value === null && (
              <span className="block text-xs text-amber-800">
                ambiguous
                {field.normalized_value.alternatives ? `: ${field.normalized_value.alternatives.join(" or ")}` : ""}
              </span>
            )}
            {field.alternatives.map((alternative) => (
              <span key={`${alternative.origin}-${alternative.value}`} className="block text-xs text-amber-800">
                {alternative.origin === "LLM" ? "AI model read" : "Layout rules read"}: {alternative.value}
              </span>
            ))}
          </>
        ) : (
          <span className="text-slate-400">not found</span>
        )}
        {editing && (
          <CorrectionForm
            documentId={documentId}
            field={field}
            onDone={() => {
              setEditing(false);
            }}
          />
        )}
      </td>
      <td className={`py-2 pr-3 align-top text-xs ${evidenceStyle(field)}`}>
        {found ? EVIDENCE_LABELS[field.corrected_value !== null ? "HUMAN" : field.evidence_status] : "—"}
        {field.page_number !== null && <span className="text-slate-500"> · p. {field.page_number}</span>}
      </td>
      <td className="py-2 pr-3 text-right align-top tabular-nums">{found ? formatPercent(field.confidence) : "—"}</td>
      <td className="py-2 text-right align-top whitespace-nowrap">
        {field.bbox && field.page_number !== null && (
          <button
            type="button"
            onClick={() => {
              onShow({ page: field.page_number ?? 1, bbox: field.bbox ?? [], label: fieldLabel(field.field_name) });
            }}
            className="rounded px-2 py-0.5 text-xs text-blue-900 hover:bg-blue-50"
          >
            Show
          </button>
        )}
        {canReview && !editing && (
          <button
            type="button"
            onClick={() => {
              setEditing(true);
            }}
            className="rounded px-2 py-0.5 text-xs text-blue-900 hover:bg-blue-50"
          >
            Correct
          </button>
        )}
      </td>
    </tr>
  );
}

function RowsTable({
  fields,
  focusFieldId,
  onShow,
}: {
  fields: ExtractedField[];
  focusFieldId: string | null;
  onShow: (highlight: Highlight) => void;
}) {
  const columns = [...new Set(fields.map((field) => field.field_name))];
  const rows = new Map<number, Map<string, ExtractedField>>();
  for (const field of fields) {
    const index = field.row_index ?? 0;
    const row = rows.get(index) ?? new Map<string, ExtractedField>();
    row.set(field.field_name, field);
    rows.set(index, row);
  }
  return (
    <div className="mt-2 overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-slate-500">
          <tr>
            {columns.map((column) => (
              <th key={column} className="py-1.5 pr-3 font-medium">
                {fieldLabel(column)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {[...rows.entries()]
            .sort(([a], [b]) => a - b)
            .map(([index, row]) => (
              <tr key={index}>
                {columns.map((column) => {
                  const cell = row.get(column);
                  const weak = cell !== undefined && Number(cell.confidence) < ATTENTION_BELOW;
                  const focused = cell !== undefined && cell.id === focusFieldId;
                  return (
                    <td
                      key={column}
                      aria-current={focused || undefined}
                      className={`py-1.5 pr-3 ${focused ? "bg-blue-50 ring-2 ring-blue-300" : weak ? "bg-amber-50" : ""}`}
                    >
                      {cell ? (
                        <button
                          type="button"
                          disabled={!cell.bbox || cell.page_number === null}
                          title={`${EVIDENCE_LABELS[cell.evidence_status]} · confidence ${formatPercent(cell.confidence)}`}
                          onClick={() => {
                            onShow({ page: cell.page_number ?? 1, bbox: cell.bbox ?? [], label: fieldLabel(column) });
                          }}
                          className="text-left hover:underline disabled:no-underline"
                        >
                          {fieldValue(cell)}
                        </button>
                      ) : (
                        <span className="text-slate-300">—</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  );
}

export function ExtractionSection({
  documentId,
  focusFieldId = null,
  onShow,
}: {
  documentId: string;
  /** A field to point at (links from comparisons carry ?field=<id>): highlighted and shown on its page. */
  focusFieldId?: string | null;
  onShow: (highlight: Highlight) => void;
}) {
  const { token, user } = useAuth();
  const canReview = user?.permissions.includes("documents:review") ?? false;
  const extraction = useQuery({
    queryKey: ["documents", "extraction", documentId],
    queryFn: ({ signal }) => apiRequest<Extraction>(`/api/v1/documents/${documentId}/extraction`, { token, signal }),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 2,
  });
  const focused = focusFieldId ? extraction.data?.fields.find((field) => field.id === focusFieldId) : undefined;
  const shownFocus = useRef<string | null>(null);
  useEffect(() => {
    // Once per focused field: onShow changes identity on every parent render.
    if (!focused || shownFocus.current === focused.id) return;
    shownFocus.current = focused.id;
    if (focused.bbox && focused.page_number !== null) {
      onShow({ page: focused.page_number, bbox: focused.bbox, label: fieldLabel(focused.field_name) });
    }
  }, [focused, onShow]);

  if (extraction.isPending) {
    return null;
  }
  if (extraction.isError) {
    const none = extraction.error instanceof ApiError && extraction.error.status === 404;
    return (
      <section aria-labelledby="extraction-heading" className="rounded-xl border border-slate-200 bg-white p-5">
        <h2 id="extraction-heading" className="font-medium">
          Extracted data
        </h2>
        <p className="mt-2 text-sm text-slate-500">
          {none ? "No structured fields are extracted for this document type." : "Extracted data could not be loaded."}
        </p>
      </section>
    );
  }

  const data = extraction.data;
  const header = data.fields.filter((field) => field.group_name === null);
  const groups = [...new Set(data.fields.flatMap((field) => (field.group_name ? [field.group_name] : [])))];
  const llm = data.signals.llm;
  const failed = data.checks.filter((check) => check.status === "FAIL");

  return (
    <section aria-labelledby="extraction-heading" className="rounded-xl border border-slate-200 bg-white p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="extraction-heading" className="font-medium">
            Extracted data
          </h2>
          <p className="mt-1 text-xs text-slate-500">
            {fieldLabel(data.schema_name)} schema v{data.schema_version} · {EXTRACTION_METHOD_LABELS[data.method] ?? data.method}
            {data.vendor && ` · vendor: ${data.vendor.canonical_name}`}
          </p>
          <p className="mt-0.5 text-xs text-slate-500">
            {llm?.used
              ? `AI model ${llm.model ?? ""} consulted${llm.cache_hit ? " (cached result)" : ""}`
              : `AI model not used: ${llm?.reason ?? "—"}`}
          </p>
        </div>
        <div className="text-right">
          <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${REVIEW_LEVEL_STYLES[data.review_level]}`}>
            {REVIEW_LEVEL_LABELS[data.review_level]}
          </span>
          <p className="mt-1 text-xs text-slate-500 tabular-nums">confidence {formatPercent(data.overall_confidence)}</p>
        </div>
      </div>

      {failed.length > 0 && (
        <ul role="alert" className="mt-3 space-y-1 rounded-md bg-red-50 px-3 py-2 text-sm text-red-800">
          {failed.map((check) => (
            <li key={`${check.code}-${check.fields.join(",")}`}>{check.message}</li>
          ))}
        </ul>
      )}

      <table className="mt-4 w-full text-left text-sm">
        <thead className="text-xs uppercase text-slate-500">
          <tr>
            <th className="py-1.5 pr-3 font-medium">Field</th>
            <th className="py-1.5 pr-3 font-medium">Value</th>
            <th className="py-1.5 pr-3 font-medium">Evidence</th>
            <th className="py-1.5 pr-3 text-right font-medium">Confidence</th>
            <th className="py-1.5" />
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {header.map((field) => (
            <FieldRow
              key={field.id}
              documentId={documentId}
              field={field}
              canReview={canReview}
              focused={field.id === focusFieldId}
              onShow={onShow}
            />
          ))}
        </tbody>
      </table>

      {groups.map((group) => (
        <div key={group} className="mt-5">
          <h3 className="text-xs uppercase text-slate-500">{fieldLabel(group)}</h3>
          <RowsTable
            fields={data.fields.filter((field) => field.group_name === group)}
            focusFieldId={focusFieldId}
            onShow={onShow}
          />
        </div>
      ))}

      {data.checks.length > 0 && (
        <details className="mt-4 text-sm">
          <summary className="cursor-pointer text-slate-600">
            Consistency checks ({data.checks.length - failed.length} passed, {failed.length} failed)
          </summary>
          <ul className="mt-2 space-y-1 text-slate-600">
            {data.checks.map((check) => (
              <li key={`${check.code}-${check.fields.join(",")}`}>
                <span className={check.status === "PASS" ? "text-emerald-700" : "text-red-700"}>
                  {check.status === "PASS" ? "✓" : "✗"}
                </span>{" "}
                {check.message}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
