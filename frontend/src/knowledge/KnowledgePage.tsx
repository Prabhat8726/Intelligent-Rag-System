import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth/useAuth";
import { apiRequest } from "../lib/api";
import type { KnowledgeCategory, KnowledgeDocument, KnowledgeStatus } from "../lib/types";
import { AskPanel } from "./AskPanel";
import {
  CATEGORIES,
  CATEGORY_LABELS,
  type KnowledgeFilters,
  knowledgeStatusLabel,
  knowledgeStatusStyle,
  knowledgeUrl,
  formatPeriod,
  PAGE_SIZE,
} from "./format";
import { KnowledgeUploadForm } from "./KnowledgeUploadForm";

interface KnowledgePageData {
  items: KnowledgeDocument[];
  total: number;
  limit: number;
  offset: number;
}

const STATUSES: KnowledgeStatus[] = ["ACTIVE", "SUPERSEDED", "PROCESSING", "FAILED"];

export function KnowledgePage() {
  const { token, user } = useAuth();
  const canManage = user?.permissions.includes("knowledge:manage") ?? false;
  const [filters, setFilters] = useState<KnowledgeFilters>({ status: "", category: "", q: "", offset: 0 });
  const documents = useQuery({
    queryKey: ["knowledge", filters],
    queryFn: ({ signal }) => apiRequest<KnowledgePageData>(knowledgeUrl(filters), { token, signal }),
    placeholderData: keepPreviousData,
    // Refresh while documents are being processed.
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => item.status === "PROCESSING") ? 3000 : false,
  });
  const update = (change: Partial<KnowledgeFilters>) => {
    setFilters({ ...filters, offset: 0, ...change });
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Knowledge base</h1>
        <p className="mt-1 text-sm text-slate-600">
          Answers come only from the documents below, with sources. Superseded versions are cited only for
          dates when they were in force; department documents are visible to that department only.
        </p>
      </div>
      <AskPanel />
      {canManage && <KnowledgeUploadForm />}
      <section aria-label="Library" className="rounded-xl border border-slate-200 bg-white">
        <div className="flex flex-wrap items-end gap-3 border-b border-slate-200 p-4 text-xs">
          <label>
            <span className="block text-slate-500">Title contains</span>
            <input
              value={filters.q}
              maxLength={200}
              onChange={(event) => {
                update({ q: event.target.value });
              }}
              className="mt-1 rounded-md border border-slate-300 px-2 py-1 text-sm"
            />
          </label>
          <label>
            <span className="block text-slate-500">Category</span>
            <select
              value={filters.category}
              onChange={(event) => {
                update({ category: event.target.value as KnowledgeCategory | "" });
              }}
              className="mt-1 rounded-md border border-slate-300 px-2 py-1 text-sm"
            >
              <option value="">All</option>
              {CATEGORIES.map((value) => (
                <option key={value} value={value}>
                  {CATEGORY_LABELS[value]}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="block text-slate-500">Status</span>
            <select
              value={filters.status}
              onChange={(event) => {
                update({ status: event.target.value as KnowledgeStatus | "" });
              }}
              className="mt-1 rounded-md border border-slate-300 px-2 py-1 text-sm"
            >
              <option value="">All</option>
              {STATUSES.map((value) => (
                <option key={value} value={value}>
                  {knowledgeStatusLabel(value)}
                </option>
              ))}
            </select>
          </label>
        </div>
        {documents.isError && (
          <p role="alert" className="p-4 text-sm text-red-700">
            The knowledge base could not be loaded.
          </p>
        )}
        {documents.data && documents.data.items.length === 0 && (
          <p className="p-4 text-sm text-slate-600">No knowledge documents match.</p>
        )}
        {documents.data && documents.data.items.length > 0 && (
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-50 text-xs uppercase text-slate-500">
              <tr>
                <th className="px-4 py-2">Title</th>
                <th className="px-4 py-2">Category</th>
                <th className="px-4 py-2">Version</th>
                <th className="px-4 py-2">In force</th>
                <th className="px-4 py-2">Audience</th>
                <th className="px-4 py-2">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {documents.data.items.map((item) => (
                <tr key={item.id}>
                  <td className="px-4 py-2">
                    <Link to={`/knowledge/${item.id}`} className="font-medium text-blue-900 hover:underline">
                      {item.title}
                    </Link>
                    <div className="text-xs text-slate-500">{item.document_key}</div>
                  </td>
                  <td className="px-4 py-2">{CATEGORY_LABELS[item.category]}</td>
                  <td className="px-4 py-2">{item.version_label ?? "—"}</td>
                  <td className="px-4 py-2 text-xs">{formatPeriod(item.effective_from, item.effective_to) || "always"}</td>
                  <td className="px-4 py-2 text-xs">{item.department ? item.department.name : "Everyone"}</td>
                  <td className="px-4 py-2">
                    <span className={`rounded px-2 py-0.5 text-xs ${knowledgeStatusStyle(item.status)}`}>
                      {knowledgeStatusLabel(item.status)}
                    </span>
                    {item.status !== "PROCESSING" && !item.embedding_model && item.status !== "FAILED" && (
                      <div className="mt-1 text-xs text-slate-500" title={item.embedding_note ?? undefined}>
                        full-text only
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {documents.data && documents.data.total > PAGE_SIZE && (
          <div className="flex items-center justify-between border-t border-slate-200 p-3 text-sm">
            <span>
              {filters.offset + 1}–{Math.min(filters.offset + PAGE_SIZE, documents.data.total)} of{" "}
              {documents.data.total}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={filters.offset === 0}
                onClick={() => {
                  setFilters({ ...filters, offset: Math.max(0, filters.offset - PAGE_SIZE) });
                }}
                className="rounded-md border border-slate-300 px-3 py-1 disabled:opacity-50"
              >
                Previous
              </button>
              <button
                type="button"
                disabled={filters.offset + PAGE_SIZE >= documents.data.total}
                onClick={() => {
                  setFilters({ ...filters, offset: filters.offset + PAGE_SIZE });
                }}
                className="rounded-md border border-slate-300 px-3 py-1 disabled:opacity-50"
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
