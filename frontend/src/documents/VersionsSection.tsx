import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type SubmitEvent, useRef, useState } from "react";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { ClauseDiff, DocumentSummary, VersionComparison, VersionInfo } from "../lib/types";
import { CHANGE_LABELS, CHANGE_STYLES } from "../review/format";
import { formatBytes, formatDateTime } from "./format";

const ACCEPT = ".pdf,.png,.jpg,.jpeg,.tif,.tiff";
const CHANGE_ORDER: ClauseDiff["change"][] = ["MODIFIED", "ADDED", "REMOVED", "UNCHANGED"];

function errorText(error: unknown): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : "The request failed.";
}

function WordDiff({ operations }: { operations: ClauseDiff["operations"] }) {
  return (
    <p className="mt-2 text-sm leading-relaxed text-slate-800">
      {operations.map((operation, index) => {
        const key = `${String(index)}-${operation.op}`;
        switch (operation.op) {
          case "equal":
            return <span key={key}>{operation.new} </span>;
          case "insert":
            return (
              <ins key={key} className="bg-emerald-100 text-emerald-900 no-underline">
                {operation.new}{" "}
              </ins>
            );
          case "delete":
            return (
              <del key={key} className="bg-red-100 text-red-900">
                {operation.old}{" "}
              </del>
            );
          default:
            return (
              <span key={key}>
                <del className="bg-red-100 text-red-900">{operation.old}</del>{" "}
                <ins className="bg-emerald-100 text-emerald-900 no-underline">{operation.new}</ins>{" "}
              </span>
            );
        }
      })}
    </p>
  );
}

function clauseHeading(clause: ClauseDiff): string {
  const current = clause.new ?? clause.old;
  const number = current?.number ? `${current.number} ` : "";
  return `${number}${clause.title}`;
}

function ClauseChange({ clause }: { clause: ClauseDiff }) {
  return (
    <li aria-label={clauseHeading(clause)} className="py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${CHANGE_STYLES[clause.change]}`}>
          {CHANGE_LABELS[clause.change]}
        </span>
        <span className="text-sm font-medium">{clauseHeading(clause)}</span>
        {clause.renumbered && clause.old?.number && clause.new?.number && (
          <span className="text-xs text-slate-500">
            renumbered {clause.old.number} → {clause.new.number}
          </span>
        )}
      </div>
      {clause.change === "MODIFIED" && <WordDiff operations={clause.operations} />}
      {clause.change === "ADDED" && clause.new && (
        <p className="mt-2 bg-emerald-50 text-sm text-emerald-900">{clause.new.text}</p>
      )}
      {clause.change === "REMOVED" && clause.old && (
        <p className="mt-2 bg-red-50 text-sm text-red-900 line-through">{clause.old.text}</p>
      )}
    </li>
  );
}

function VersionDiff({ documentId, from, to }: { documentId: string; from: number; to: number }) {
  const { token } = useAuth();
  const url = `/api/v1/documents/${documentId}/versions/compare?from=${String(from)}&to=${String(to)}`;
  const diff = useQuery({
    queryKey: ["documents", "version-diff", documentId, from, to],
    queryFn: ({ signal }) => apiRequest<VersionComparison>(url, { token, signal }),
  });
  if (diff.isPending) return <p className="mt-3 text-sm text-slate-500">Comparing…</p>;
  if (diff.isError) {
    return (
      <p role="alert" className="mt-3 text-sm text-red-700">
        {errorText(diff.error)}
      </p>
    );
  }
  const changed = diff.data.clauses.filter((clause) => clause.change !== "UNCHANGED");
  const unchanged = diff.data.clauses.length - changed.length;
  return (
    <div aria-label={`Changes from version ${String(from)} to ${String(to)}`} className="mt-3">
      <div className="flex flex-wrap gap-2 text-xs">
        {CHANGE_ORDER.map((change) => (
          <span key={change} className={`rounded-full px-2.5 py-0.5 ${CHANGE_STYLES[change]}`}>
            {diff.data.summary[change]} {CHANGE_LABELS[change].toLowerCase()}
          </span>
        ))}
      </div>
      {changed.length === 0 ? (
        <p className="mt-3 text-sm text-slate-500">No clause changed between these versions.</p>
      ) : (
        <ul className="mt-2 divide-y divide-slate-100">
          {changed.map((clause) => (
            <ClauseChange key={`${clause.change}-${clause.old?.key ?? ""}-${clause.new?.key ?? ""}`} clause={clause} />
          ))}
        </ul>
      )}
      {unchanged > 0 && <p className="mt-1 text-xs text-slate-500">{unchanged} clause(s) unchanged.</p>}
    </div>
  );
}

function NewVersionForm({ documentId, disabled }: { documentId: string; disabled: boolean }) {
  const { token } = useAuth();
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const upload = useMutation({
    mutationFn: (selected: File) => {
      const form = new FormData();
      form.append("file", selected);
      return apiRequest<DocumentSummary>(`/api/v1/documents/${documentId}/versions`, {
        method: "POST",
        body: form,
        token,
      });
    },
    onSuccess: async () => {
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
  });
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (file) upload.mutate(file);
  };
  return (
    <form aria-label="Upload new version" onSubmit={submit} className="mt-4 flex flex-wrap items-end gap-3">
      <label className="text-xs">
        <span className="block text-slate-500">New version of this document</span>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          onChange={(event) => {
            setFile(event.target.files?.[0] ?? null);
            upload.reset();
          }}
          className="mt-1 block text-sm file:mr-3 file:rounded-md file:border-0 file:bg-slate-100 file:px-3 file:py-1.5 file:text-sm"
        />
      </label>
      <button
        type="submit"
        disabled={!file || disabled || upload.isPending}
        className="rounded-md bg-blue-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
      >
        {upload.isPending ? "Uploading…" : "Upload version"}
      </button>
      {disabled && <span className="text-xs text-slate-500">Available once processing has finished.</span>}
      {upload.isError && (
        <p role="alert" className="w-full text-sm text-red-700">
          {errorText(upload.error)}
        </p>
      )}
      {upload.isSuccess && (
        <p role="status" className="w-full text-sm text-emerald-700">
          Version uploaded — queued for processing.
        </p>
      )}
    </form>
  );
}

export function VersionsSection({ documentId, busy }: { documentId: string; busy: boolean }) {
  const { token, user } = useAuth();
  const canUpload = user?.permissions.includes("documents:upload") ?? false;
  const versions = useQuery({
    queryKey: ["documents", "versions", documentId],
    queryFn: ({ signal }) => apiRequest<VersionInfo[]>(`/api/v1/documents/${documentId}/versions`, { token, signal }),
  });
  const [from, setFrom] = useState<number | null>(null);
  const [to, setTo] = useState<number | null>(null);
  const [shown, setShown] = useState<{ from: number; to: number } | null>(null);

  if (versions.isPending || versions.isError) return null;
  const list = versions.data;
  const processed = list.filter((version) => version.processed);
  // Defaults: the newest processed version against the one before it.
  const toValue = to ?? processed[0]?.version_number ?? null;
  const fromValue = from ?? processed[1]?.version_number ?? null;

  const select = (label: string, value: number | null, onChange: (value: number) => void) => (
    <label className="text-xs">
      <span className="block text-slate-500">{label}</span>
      <select
        value={value ?? ""}
        onChange={(event) => {
          onChange(Number(event.target.value));
        }}
        className="mt-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-sm"
      >
        {processed.map((version) => (
          <option key={version.id} value={version.version_number}>
            Version {version.version_number}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <section aria-labelledby="versions-heading" className="rounded-xl border border-slate-200 bg-white p-5">
      <h2 id="versions-heading" className="font-medium">
        Versions
      </h2>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-xs uppercase text-slate-500">
            <tr>
              <th className="py-1.5 font-medium">Version</th>
              <th className="py-1.5 font-medium">File</th>
              <th className="py-1.5 font-medium">Uploaded</th>
              <th className="py-1.5 text-right font-medium">Size</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {list.map((version) => (
              <tr key={version.id}>
                <td className="py-1.5">
                  {version.version_number}
                  {version.is_current && <span className="ml-2 text-xs text-slate-500">current</span>}
                  {!version.processed && <span className="ml-2 text-xs text-amber-700">not processed</span>}
                </td>
                <td className="py-1.5 break-all">{version.original_filename}</td>
                <td className="py-1.5 text-slate-600">{formatDateTime(version.created_at)}</td>
                <td className="py-1.5 text-right tabular-nums">{formatBytes(version.size_bytes)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {processed.length >= 2 && fromValue !== null && toValue !== null && (
        <div className="mt-4 border-t border-slate-100 pt-4">
          <h3 className="text-sm font-medium">Compare versions</h3>
          <div className="mt-2 flex flex-wrap items-end gap-3">
            {select("From", fromValue, setFrom)}
            {select("To", toValue, setTo)}
            <button
              type="button"
              disabled={fromValue === toValue}
              onClick={() => {
                setShown({ from: fromValue, to: toValue });
              }}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-40"
            >
              Compare
            </button>
          </div>
          {shown && <VersionDiff documentId={documentId} from={shown.from} to={shown.to} />}
        </div>
      )}

      {canUpload && <NewVersionForm documentId={documentId} disabled={busy} />}
    </section>
  );
}
