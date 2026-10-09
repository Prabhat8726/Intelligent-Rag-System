import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest, downloadFile } from "../lib/api";
import type { DocumentDetail, Highlight, ProcessingJob } from "../lib/types";
import { PriorityBadge } from "../review/Badges";
import { ClassificationCard } from "./ClassificationCard";
import { ExtractionSection } from "./ExtractionSection";
import { FindingsSection } from "./FindingsSection";
import {
  ACTIVE_STATUSES,
  formatBytes,
  formatDateTime,
  formatDuration,
  INSPECTION_LABELS,
  REVIEW_REASON_LABELS,
} from "./format";
import { PageViewer } from "./PageViewer";
import { DocumentStatusBadge } from "./StatusBadge";
import { TablesSection } from "./TablesSection";
import { VersionsSection } from "./VersionsSection";

const ACTIVE_REFRESH_MS = 2000;

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium break-all">{children}</dd>
    </div>
  );
}

function errorText(error: unknown): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : "The request failed.";
}

export function DocumentDetailPage() {
  const { documentId = "" } = useParams();
  const [search] = useSearchParams();
  const { token, user } = useAuth();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [actionError, setActionError] = useState<string | null>(null);
  const [selectedPage, setSelectedPage] = useState(1);
  const [highlight, setHighlight] = useState<Highlight | null>(null);
  const can = (permission: string) => user?.permissions.includes(permission) ?? false;
  const url = `/api/v1/documents/${encodeURIComponent(documentId)}`;

  const detail = useQuery({
    queryKey: ["documents", "detail", documentId],
    queryFn: ({ signal }) => apiRequest<DocumentDetail>(url, { token, signal }),
    refetchInterval: (current) =>
      current.state.data && ACTIVE_STATUSES.has(current.state.data.status) ? ACTIVE_REFRESH_MS : false,
  });

  const reprocess = useMutation({
    mutationFn: () => apiRequest<ProcessingJob>(`${url}/process`, { method: "POST", token }),
    onSuccess: () => {
      setActionError(null);
      void queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
    onError: (error) => {
      setActionError(errorText(error));
    },
  });

  const remove = useMutation({
    mutationFn: () => apiRequest<null>(url, { method: "DELETE", token }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
      await navigate("/documents", { replace: true });
    },
    onError: (error) => {
      setActionError(errorText(error));
    },
  });

  if (detail.isPending) {
    return (
      <p role="status" className="text-sm text-slate-500">
        Loading document…
      </p>
    );
  }
  if (detail.isError) {
    const notFound = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <div role="alert" className="rounded-xl border border-slate-200 bg-white p-6">
        <p className="font-medium">{notFound ? "Document not found." : "The document could not be loaded."}</p>
        <Link to="/documents" className="mt-2 inline-block text-sm text-blue-900 underline">
          Back to documents
        </Link>
      </div>
    );
  }

  const document = detail.data;
  const version = document.current_version;
  const job = document.latest_job;
  const inspection = document.inspection;
  const detected = document.sensitivity_assessment?.detected ?? null;
  const processed = !ACTIVE_STATUSES.has(document.status) && document.status !== "FAILED";

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Link to="/documents" className="text-sm text-blue-900 hover:underline">
            ← Documents
          </Link>
          <h1 className="mt-1 text-xl font-semibold break-all">{document.display_filename}</h1>
          <div className="mt-2 flex items-center gap-3">
            <DocumentStatusBadge status={document.status} />
            <span className="text-xs text-slate-500">{document.sensitivity.toLowerCase()} · {document.source.toLowerCase()}</span>
          </div>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => {
              downloadFile(`${url}/file`, token, document.display_filename).catch((error: unknown) => {
                setActionError(errorText(error));
              });
            }}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100"
          >
            Download original
          </button>
          {can("documents:process") && (
            <button
              type="button"
              disabled={reprocess.isPending || ACTIVE_STATUSES.has(document.status)}
              onClick={() => {
                reprocess.mutate();
              }}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-40"
            >
              Reprocess
            </button>
          )}
          {can("documents:delete") && (
            <button
              type="button"
              disabled={remove.isPending}
              onClick={() => {
                if (window.confirm("Delete this document? It will be hidden from all users.")) remove.mutate();
              }}
              className="rounded-md border border-red-200 px-3 py-1.5 text-sm text-red-700 hover:bg-red-50"
            >
              Delete
            </button>
          )}
        </div>
      </div>

      {actionError && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {actionError}
        </p>
      )}
      {document.processing_error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          Processing failed: {document.processing_error}
        </p>
      )}
      {document.status === "REVIEW_REQUIRED" && (
        <div role="status" className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <p className="font-medium">
            Needs review
            {document.review && (
              <>
                {" · "}
                <PriorityBadge priority={document.review.priority} />
              </>
            )}
          </p>
          <ul className="mt-1 list-disc pl-5">
            {document.review_reasons.map((reason) => (
              <li key={reason}>{REVIEW_REASON_LABELS[reason] ?? reason}</li>
            ))}
          </ul>
          <a href="#findings-heading" className="mt-1 inline-block text-amber-900 underline">
            See the findings and record a decision
          </a>
        </div>
      )}
      {document.duplicate_of_id && (
        <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800">
          Identical file already uploaded:{" "}
          <Link to={`/documents/${document.duplicate_of_id}`} className="underline">
            view original
          </Link>
        </p>
      )}

      <section aria-labelledby="file-heading" className="rounded-xl border border-slate-200 bg-white p-5">
        <h2 id="file-heading" className="font-medium">
          File
        </h2>
        <dl className="mt-4 grid grid-cols-2 gap-4 md:grid-cols-4">
          <Field label="Type">{version?.mime_type ?? "—"}</Field>
          <Field label="Pages">{version?.page_count ?? "—"}</Field>
          <Field label="Size">{version ? formatBytes(version.size_bytes) : "—"}</Field>
          <Field label="Version">{version?.version_number ?? "—"}</Field>
          <Field label="Uploaded by">{document.owner.full_name}</Field>
          <Field label="Department">{document.department?.name ?? "—"}</Field>
          <Field label="Uploaded">{formatDateTime(document.created_at)}</Field>
          <Field label="Last processed">{formatDateTime(document.last_processed_at)}</Field>
          {detected && detected !== document.sensitivity && (
            <Field label="Detected sensitivity">
              {detected.toLowerCase()}
              <span className="block text-xs font-normal text-slate-500">
                {document.sensitivity_assessment?.findings
                  .filter((finding) => finding.level !== null)
                  .map((finding) => finding.kind.replace("_", " ").toLowerCase())
                  .concat(document.sensitivity_assessment.type_minimum ? ["document type"] : [])
                  .join(", ")}
              </span>
            </Field>
          )}
          <div className="col-span-2 md:col-span-4">
            <Field label="SHA-256">
              <span className="font-mono text-xs">{version?.sha256 ?? "—"}</span>
            </Field>
          </div>
        </dl>
      </section>

      {processed && <ClassificationCard document={document} />}

      {processed && (
        <ExtractionSection
          documentId={document.id}
          focusFieldId={search.get("field")}
          onShow={(target) => {
            setHighlight(target);
            setSelectedPage(target.page);
            window.document.getElementById("pages-heading")?.scrollIntoView({ behavior: "smooth" });
          }}
        />
      )}

      {document.pages.length > 0 && (
        <section aria-labelledby="pages-heading" className="rounded-xl border border-slate-200 bg-white p-5">
          <h2 id="pages-heading" className="font-medium">
            Pages
          </h2>
          <PageViewer
            documentId={document.id}
            pages={document.pages}
            selected={selectedPage}
            onSelect={setSelectedPage}
            highlight={highlight}
          />
        </section>
      )}

      {document.pages.length > 0 && <TablesSection documentId={document.id} />}

      {processed && <FindingsSection documentId={document.id} />}

      <VersionsSection documentId={document.id} busy={ACTIVE_STATUSES.has(document.status)} />

      <section aria-labelledby="processing-heading" className="rounded-xl border border-slate-200 bg-white p-5">
        <h2 id="processing-heading" className="font-medium">
          Processing
        </h2>
        {job ? (
          <dl className="mt-4 grid grid-cols-2 gap-4 md:grid-cols-4">
            <Field label="Job status">{job.status.toLowerCase()}</Field>
            <Field label="Attempts">
              {job.attempts} / {job.max_attempts}
            </Field>
            <Field label="Duration">{formatDuration(job.duration_ms)}</Field>
            <Field label="Stage">{job.stage ?? "—"}</Field>
            {Object.entries(job.stage_timings).map(([stage, ms]) => (
              <Field key={stage} label={`Stage: ${stage}`}>
                {formatDuration(Math.round(ms))}
              </Field>
            ))}
            {job.last_error && (
              <div className="col-span-2 md:col-span-4">
                <Field label="Last error">{job.last_error}</Field>
              </div>
            )}
          </dl>
        ) : (
          <p className="mt-2 text-sm text-slate-500">No processing jobs yet.</p>
        )}
      </section>

      <section aria-labelledby="inspection-heading" className="rounded-xl border border-slate-200 bg-white p-5">
        <h2 id="inspection-heading" className="font-medium">
          Page inspection
        </h2>
        {inspection ? (
          <>
            <p className="mt-2 text-sm text-slate-600">
              {INSPECTION_LABELS[inspection.kind] ?? inspection.kind}
              {inspection.pages_needing_ocr.length > 0 &&
                ` · OCR needed on ${inspection.pages_needing_ocr.length} of ${inspection.page_count} page(s)`}
            </p>
            <table className="mt-3 w-full text-left text-sm">
              <thead className="text-xs uppercase text-slate-500">
                <tr>
                  <th className="py-2 font-medium">Page</th>
                  <th className="py-2 font-medium">Text source</th>
                  <th className="py-2 text-right font-medium">Text characters</th>
                  <th className="py-2 text-right font-medium">Images</th>
                  <th className="py-2 text-right font-medium">Size</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {inspection.pages.map((page) => (
                  <tr key={page.page_number}>
                    <td className="py-1.5">{page.page_number}</td>
                    <td className="py-1.5">{page.method === "NATIVE" ? "Text layer" : "Needs OCR"}</td>
                    <td className="py-1.5 text-right tabular-nums">{page.text_chars}</td>
                    <td className="py-1.5 text-right tabular-nums">{page.image_objects}</td>
                    <td className="py-1.5 text-right tabular-nums text-slate-600">
                      {Math.round(page.width)} × {Math.round(page.height)} {page.unit}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <p className="mt-2 text-sm text-slate-500">Not inspected yet.</p>
        )}
      </section>
    </div>
  );
}
