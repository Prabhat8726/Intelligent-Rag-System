import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { useAuth } from "../auth/useAuth";
import { apiRequest, fetchBlob } from "../lib/api";
import type { Highlight, PageDetail, PageSummary } from "../lib/types";

const LOW_CONFIDENCE = 60;

function usePreviewUrl(
  documentId: string,
  page: PageSummary | undefined,
): { url: string | null; failed: boolean } {
  const { token } = useAuth();
  const image = useQuery({
    queryKey: ["documents", "page-image", documentId, page?.page_number],
    queryFn: ({ signal }) =>
      fetchBlob(`/api/v1/documents/${documentId}/pages/${String(page?.page_number)}/image`, token, signal),
    enabled: page?.has_preview ?? false,
    staleTime: Infinity,
  });
  const url = useMemo(() => (image.data ? URL.createObjectURL(image.data) : null), [image.data]);
  useEffect(
    () => () => {
      if (url) URL.revokeObjectURL(url);
    },
    [url],
  );
  return { url, failed: image.isError };
}

export function PageViewer({
  documentId,
  pages,
  selected,
  onSelect,
  highlight,
}: {
  documentId: string;
  pages: PageSummary[];
  selected: number;
  onSelect: (page: number) => void;
  highlight: Highlight | null;
}) {
  const { token } = useAuth();
  const [showBoxes, setShowBoxes] = useState(false);
  const summary = pages.find((page) => page.page_number === selected) ?? pages[0];
  const detail = useQuery({
    queryKey: ["documents", "page", documentId, summary?.page_number],
    queryFn: ({ signal }) =>
      apiRequest<PageDetail>(`/api/v1/documents/${documentId}/pages/${String(summary?.page_number)}`, {
        token,
        signal,
      }),
    enabled: summary !== undefined,
  });
  const preview = usePreviewUrl(documentId, summary);
  const previewUrl = preview.url;

  if (!summary) {
    return <p className="mt-2 text-sm text-slate-500">No pages extracted yet.</p>;
  }
  const page = detail.data;

  return (
    <div className="mt-4 space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {pages.length > 1 && (
          <div role="tablist" aria-label="Pages" className="flex flex-wrap gap-1">
            {pages.map((item) => (
              <button
                key={item.page_number}
                type="button"
                role="tab"
                aria-selected={item.page_number === summary.page_number}
                onClick={() => {
                  onSelect(item.page_number);
                }}
                className={`rounded-md px-2.5 py-1 text-sm ${
                  item.page_number === summary.page_number
                    ? "bg-blue-900 text-white"
                    : "border border-slate-300 hover:bg-slate-100"
                }`}
              >
                {item.page_number}
              </button>
            ))}
          </div>
        )}
        <span className="text-sm text-slate-600">
          {summary.extraction_method === "NATIVE" ? "Text layer" : "OCR"}
          {summary.ocr_confidence !== null && ` · confidence ${Math.round(Number(summary.ocr_confidence))}%`}
          {summary.rotation_applied !== 0 && ` · turned ${String(summary.rotation_applied)}°`} · {summary.word_count} words
        </span>
        <label className="ml-auto flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={showBoxes}
            onChange={(event) => {
              setShowBoxes(event.target.checked);
            }}
          />
          Show word boxes
        </label>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="relative overflow-hidden rounded-lg border border-slate-200 bg-slate-50">
          {previewUrl ? (
            <img
              src={previewUrl}
              alt={`Page ${String(summary.page_number)} preview`}
              className="block w-full"
            />
          ) : (
            <p className="p-6 text-sm text-slate-500">
              {!summary.has_preview ? "No preview." : preview.failed ? "Preview unavailable." : "Loading preview…"}
            </p>
          )}
          {previewUrl && highlight?.page === summary.page_number && highlight.bbox.length === 4 && (
            <svg
              role="img"
              aria-label={`Source of ${highlight.label}`}
              viewBox={`0 0 ${String(summary.width)} ${String(summary.height)}`}
              preserveAspectRatio="none"
              className="pointer-events-none absolute inset-0 h-full w-full"
            >
              <rect
                x={(highlight.bbox[0] ?? 0) - 2}
                y={(highlight.bbox[1] ?? 0) - 2}
                width={(highlight.bbox[2] ?? 0) - (highlight.bbox[0] ?? 0) + 4}
                height={(highlight.bbox[3] ?? 0) - (highlight.bbox[1] ?? 0) + 4}
                fill="rgba(245, 158, 11, 0.25)"
                stroke="#d97706"
                strokeWidth={2}
                vectorEffect="non-scaling-stroke"
              />
            </svg>
          )}
          {previewUrl && showBoxes && page && (
            <svg
              aria-hidden="true"
              viewBox={`0 0 ${String(page.width)} ${String(page.height)}`}
              preserveAspectRatio="none"
              className="pointer-events-none absolute inset-0 h-full w-full"
            >
              {page.words.map(([text, x0, y0, x1, y1, confidence], index) => (
                <rect
                  key={`${String(index)}-${text}`}
                  x={x0}
                  y={y0}
                  width={x1 - x0}
                  height={y1 - y0}
                  fill="none"
                  strokeWidth={0.6}
                  stroke={confidence !== null && confidence < LOW_CONFIDENCE ? "#dc2626" : "#1e3a8a"}
                  vectorEffect="non-scaling-stroke"
                />
              ))}
            </svg>
          )}
        </div>
        <div>
          <h3 className="text-xs uppercase text-slate-500">Extracted text</h3>
          {detail.isPending && <p className="mt-2 text-sm text-slate-500">Loading text…</p>}
          {page && (
            <pre className="mt-2 max-h-[36rem] overflow-auto rounded-lg bg-slate-50 p-3 font-mono text-xs whitespace-pre-wrap">
              {page.text || "No text found on this page."}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
