import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth/useAuth";
import { apiRequest } from "../lib/api";
import type { DocumentStatus, DocumentSummary, Page } from "../lib/types";
import { ACTIVE_STATUSES, documentsUrl, formatBytes, formatDateTime, PAGE_SIZE } from "./format";
import { DocumentStatusBadge } from "./StatusBadge";
import { UploadForm } from "./UploadForm";

const ACTIVE_REFRESH_MS = 3000;
const STATUS_OPTIONS: (DocumentStatus | "")[] = ["", "PENDING", "PROCESSING", "COMPLETED", "FAILED", "REVIEW_REQUIRED"];

export function DocumentsPage() {
  const { token, user } = useAuth();
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const canUpload = user?.permissions.includes("documents:upload") ?? false;

  const documents = useQuery({
    queryKey: ["documents", status, query, offset],
    queryFn: ({ signal }) =>
      apiRequest<Page<DocumentSummary>>(documentsUrl({ status, q: query, offset }), { token, signal }),
    placeholderData: keepPreviousData,
    // Poll only while something is still being processed.
    refetchInterval: (current) =>
      current.state.data?.items.some((item) => ACTIVE_STATUSES.has(item.status)) ? ACTIVE_REFRESH_MS : false,
  });

  const page = documents.data;
  const lastIndex = page ? Math.min(page.offset + page.items.length, page.total) : 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Documents</h1>
        <p className="mt-1 text-sm text-slate-500">
          Uploaded files are validated, stored and inspected page by page. Extraction arrives in a later phase.
        </p>
      </div>

      {canUpload && <UploadForm />}

      <section aria-labelledby="inbox-heading" className="rounded-xl border border-slate-200 bg-white">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-3">
          <h2 id="inbox-heading" className="font-medium">
            Inbox
          </h2>
          <div className="flex gap-2">
            <input
              type="search"
              aria-label="Search by filename"
              placeholder="Search filename"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setOffset(0);
              }}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm"
            />
            <select
              aria-label="Filter by status"
              value={status}
              onChange={(event) => {
                setStatus(event.target.value);
                setOffset(0);
              }}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm"
            >
              {STATUS_OPTIONS.map((value) => (
                <option key={value || "all"} value={value}>
                  {value ? value.replace("_", " ").toLowerCase() : "All statuses"}
                </option>
              ))}
            </select>
          </div>
        </div>

        {documents.isPending && (
          <p role="status" className="px-5 py-6 text-sm text-slate-500">
            Loading documents…
          </p>
        )}
        {documents.isError && (
          <p role="alert" className="px-5 py-6 text-sm text-red-700">
            Documents could not be loaded.
          </p>
        )}
        {page && page.items.length === 0 && (
          <p className="px-5 py-6 text-sm text-slate-500">No documents match.</p>
        )}
        {page && page.items.length > 0 && (
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase text-slate-500">
              <tr>
                <th className="px-5 py-2 font-medium">Document</th>
                <th className="py-2 font-medium">Status</th>
                <th className="py-2 text-right font-medium">Pages</th>
                <th className="py-2 text-right font-medium">Size</th>
                <th className="py-2 pl-6 font-medium">Uploaded by</th>
                <th className="px-5 py-2 font-medium">Uploaded</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {page.items.map((document) => (
                <tr key={document.id} className="hover:bg-slate-50">
                  <td className="px-5 py-2">
                    <Link to={`/documents/${document.id}`} className="font-medium text-blue-900 hover:underline">
                      {document.display_filename}
                    </Link>
                    {document.duplicate_of_id && (
                      <span className="ml-2 rounded bg-amber-50 px-1.5 py-0.5 text-xs text-amber-800">duplicate</span>
                    )}
                  </td>
                  <td className="py-2">
                    <DocumentStatusBadge status={document.status} />
                  </td>
                  <td className="py-2 text-right tabular-nums">{document.current_version?.page_count ?? "—"}</td>
                  <td className="py-2 text-right tabular-nums">
                    {document.current_version ? formatBytes(document.current_version.size_bytes) : "—"}
                  </td>
                  <td className="py-2 pl-6 text-slate-600">{document.owner.full_name}</td>
                  <td className="px-5 py-2 text-slate-600">{formatDateTime(document.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {page && page.total > 0 && (
          <div className="flex items-center justify-between border-t border-slate-100 px-5 py-3 text-sm text-slate-600">
            <span>
              {page.offset + 1}–{lastIndex} of {page.total}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={offset === 0}
                onClick={() => {
                  setOffset(Math.max(0, offset - PAGE_SIZE));
                }}
                className="rounded-md border border-slate-300 px-3 py-1 disabled:opacity-40"
              >
                Previous
              </button>
              <button
                type="button"
                disabled={lastIndex >= page.total}
                onClick={() => {
                  setOffset(offset + PAGE_SIZE);
                }}
                className="rounded-md border border-slate-300 px-3 py-1 disabled:opacity-40"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
