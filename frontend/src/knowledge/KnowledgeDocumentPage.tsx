import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router";

import { useAuth } from "../auth/useAuth";
import { formatBytes, formatDateTime } from "../documents/format";
import { ApiError, apiRequest } from "../lib/api";
import type { KnowledgeChunk, KnowledgeDocumentDetail } from "../lib/types";
import { CATEGORY_LABELS, formatPeriod, knowledgeStatusLabel, knowledgeStatusStyle } from "./format";

export function KnowledgeDocumentPage() {
  const { documentId = "" } = useParams();
  const { token, user } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const canManage = user?.permissions.includes("knowledge:manage") ?? false;
  const base = `/api/v1/knowledge/documents/${encodeURIComponent(documentId)}`;
  const document = useQuery({
    queryKey: ["knowledge", "document", documentId],
    queryFn: ({ signal }) => apiRequest<KnowledgeDocumentDetail>(base, { token, signal }),
    refetchInterval: (query) => (query.state.data?.status === "PROCESSING" ? 3000 : false),
  });
  const chunks = useQuery({
    queryKey: ["knowledge", "chunks", documentId, document.data?.status],
    queryFn: ({ signal }) => apiRequest<KnowledgeChunk[]>(`${base}/chunks`, { token, signal }),
    enabled: document.isSuccess,
  });
  const archive = useMutation({
    mutationFn: () => apiRequest<null>(base, { method: "DELETE", token }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["knowledge"] });
      await navigate("/knowledge");
    },
  });

  if (document.isError) {
    const missing = document.error instanceof ApiError && document.error.status === 404;
    return (
      <p role="alert" className="text-sm text-red-700">
        {missing ? "This knowledge document does not exist or is not available to you." : "It could not be loaded."}
      </p>
    );
  }
  if (!document.data) return <p className="text-sm text-slate-600">Loading…</p>;
  const item = document.data;
  const period = formatPeriod(item.effective_from, item.effective_to);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Link to="/knowledge" className="text-sm text-blue-900 hover:underline">
            ← Knowledge base
          </Link>
          <h1 className="mt-1 text-xl font-semibold">{item.title}</h1>
          <p className="mt-1 text-sm text-slate-600">
            {CATEGORY_LABELS[item.category]}
            {item.version_label && ` · version ${item.version_label}`}
            {` · ${period ? `in force ${period}` : "no effective dates"}`}
            {` · ${item.department ? `${item.department.name} only` : "everyone"}`}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className={`rounded px-2 py-1 text-sm ${knowledgeStatusStyle(item.status)}`}>
            {knowledgeStatusLabel(item.status)}
          </span>
          {canManage && (
            <button
              type="button"
              onClick={() => {
                if (window.confirm(`Archive “${item.title}”? Its passages will no longer be used in answers.`)) {
                  archive.mutate();
                }
              }}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100"
            >
              Archive
            </button>
          )}
        </div>
      </div>
      {archive.isError && (
        <p role="alert" className="text-sm text-red-700">
          The document could not be archived.
        </p>
      )}
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-xl border border-slate-200 bg-white p-5 text-sm md:grid-cols-3">
        <div>
          <dt className="text-xs text-slate-500">Document key</dt>
          <dd className="font-mono text-xs">{item.document_key}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">File</dt>
          <dd>
            {item.original_filename} ({formatBytes(item.size_bytes)})
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Sensitivity</dt>
          <dd>
            {item.sensitivity}
            {item.effective_sensitivity && item.effective_sensitivity !== item.sensitivity && (
              <span className="text-amber-800"> (content: {item.effective_sensitivity})</span>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Passages</dt>
          <dd>{item.chunk_count}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Embeddings</dt>
          <dd>{item.embedding_model ?? <span title={item.embedding_note ?? undefined}>none (full-text only)</span>}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Processed</dt>
          <dd>{formatDateTime(item.processed_at)}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Uploaded by</dt>
          <dd>{item.uploaded_by.full_name}</dd>
        </div>
      </dl>
      {item.processing_error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {item.processing_error}
        </p>
      )}
      {item.embedding_note && <p className="text-sm text-slate-600">{item.embedding_note}</p>}
      <section aria-label="Passages" className="space-y-2">
        <h2 className="text-sm font-semibold text-slate-700">Passages used for answers</h2>
        {chunks.data?.length === 0 && <p className="text-sm text-slate-600">No passages.</p>}
        {chunks.data?.map((chunk) => (
          <article key={chunk.id} className="rounded-lg border border-slate-200 bg-white p-3">
            <header className="flex flex-wrap gap-2 text-xs text-slate-500">
              <span className="font-mono">#{chunk.chunk_index + 1}</span>
              <span className="font-medium text-slate-700">{chunk.section_path || chunk.heading}</span>
              {chunk.page_start !== null && <span>page {chunk.page_start}</span>}
              <span>~{chunk.token_count} tokens</span>
              {chunk.kind === "table" && <span>table</span>}
            </header>
            <p className="mt-2 whitespace-pre-wrap text-sm text-slate-800">{chunk.content}</p>
          </article>
        ))}
      </section>
    </div>
  );
}
