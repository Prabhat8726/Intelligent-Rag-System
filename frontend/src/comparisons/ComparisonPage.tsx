import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { useAuth } from "../auth/useAuth";
import { formatDateTime } from "../documents/format";
import { ApiError, apiRequest } from "../lib/api";
import type { Comparison, ComparisonItem, ComparisonSide, ItemStatus } from "../lib/types";
import { ItemStatusBadge } from "../review/Badges";
import { CHECK_LABELS, COMPARISON_TYPE_LABELS, ITEM_STATUS_STYLES, itemValue, ROLE_LABELS } from "../review/format";

const STATUS_ORDER: ItemStatus[] = ["MISMATCH", "UNCERTAIN", "MISSING", "MATCH"];

function Evidence({ side }: { side: ComparisonSide }) {
  const link = side.document_id
    ? `/documents/${side.document_id}${side.field_id ? `?field=${side.field_id}` : ""}`
    : null;
  return (
    <div className="text-xs text-slate-600">
      <span className="font-medium text-slate-700">{ROLE_LABELS[side.role]}</span>
      {side.page !== null && <span> · p. {side.page}</span>}
      {side.confidence !== null && <span> · {Math.round(side.confidence * 100)}%</span>}
      {side.corrected && <span> · corrected by a reviewer</span>}
      {side.source_text && <q className="mt-0.5 block font-mono text-[11px] text-slate-500">{side.source_text}</q>}
      {link && (
        <Link to={link} className="text-blue-900 hover:underline">
          Show in document
        </Link>
      )}
    </div>
  );
}

function ItemRow({ item }: { item: ComparisonItem }) {
  const [open, setOpen] = useState(item.status !== "MATCH");
  const label = CHECK_LABELS[item.check_name] ?? item.check_name;
  return (
    <>
      <tr className={item.status === "MATCH" ? undefined : "bg-slate-50/60"}>
        <td className="py-2 pr-3 pl-5 align-top">
          <span className="font-medium">{item.line_key ?? label}</span>
          {item.line_key && <span className="block text-xs text-slate-500">{label}</span>}
        </td>
        <td className="py-2 pr-3 align-top tabular-nums">{itemValue(item.check_name, item.left_value)}</td>
        <td className="py-2 pr-3 align-top tabular-nums">{itemValue(item.check_name, item.right_value)}</td>
        <td className="py-2 pr-3 align-top">
          <ItemStatusBadge status={item.status} />
        </td>
        <td className="py-2 pr-5 text-right align-top">
          <button
            type="button"
            aria-expanded={open}
            aria-label={`Evidence for ${item.line_key ?? label}`}
            onClick={() => {
              setOpen(!open);
            }}
            className="rounded px-2 py-0.5 text-xs text-blue-900 hover:bg-blue-50"
          >
            {open ? "Hide" : "Evidence"}
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5} className="px-5 pb-3">
            <p className="text-sm text-slate-700">{item.explanation}</p>
            <div className="mt-2 grid gap-3 sm:grid-cols-2">
              <div className="space-y-2">
                {item.left.map((side) => (
                  <Evidence key={`${side.document_id ?? ""}-${side.field_id ?? ""}`} side={side} />
                ))}
              </div>
              <div className="space-y-2">
                {item.right.map((side) => (
                  <Evidence key={`${side.document_id ?? ""}-${side.field_id ?? ""}`} side={side} />
                ))}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export function ComparisonPage() {
  const { comparisonId = "" } = useParams();
  const { token } = useAuth();
  const comparison = useQuery({
    queryKey: ["comparisons", comparisonId],
    queryFn: ({ signal }) =>
      apiRequest<Comparison>(`/api/v1/comparisons/${encodeURIComponent(comparisonId)}`, { token, signal }),
  });

  if (comparison.isPending) return <p className="text-sm text-slate-500">Loading…</p>;
  if (comparison.isError) {
    const notFound = comparison.error instanceof ApiError && comparison.error.status === 404;
    return (
      <p role="alert" className="text-sm text-red-700">
        {notFound ? "Comparison not found." : "The comparison could not be loaded."}
      </p>
    );
  }
  const data = comparison.data;
  const subject = data.documents.find((document) => document.document_id === data.subject_document_id);
  const leftRole = subject ? ROLE_LABELS[subject.role] : "Document";
  const rightRole = data.comparison_type === "INVOICE_DELIVERY" ? "Delivery notes" : "Purchase order / deliveries";
  const groups = (["HEADER", "LINE_ITEM"] as const).map((category) => ({
    category,
    items: data.items
      .filter((item) => item.category === category)
      .sort((a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status) || a.position - b.position),
  }));

  return (
    <div className="space-y-6">
      <div>
        {subject && (
          <Link to={`/documents/${subject.document_id}`} className="text-sm text-blue-900 hover:underline">
            ← {subject.display_filename}
          </Link>
        )}
        <h1 className="mt-1 text-xl font-semibold">{COMPARISON_TYPE_LABELS[data.comparison_type]}</h1>
        <p className="mt-1 text-sm text-slate-500">
          {data.origin === "AUTO" ? "Matched automatically" : `Requested by ${data.requested_by?.full_name ?? "a user"}`} ·{" "}
          {formatDateTime(data.created_at)}
        </p>
      </div>

      <section aria-label="Compared documents" className="flex flex-wrap gap-2">
        {data.documents.map((document) => (
          <Link
            key={document.document_id}
            to={`/documents/${document.document_id}`}
            className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm hover:border-blue-300"
          >
            <span className="block text-xs text-slate-500">{ROLE_LABELS[document.role]}</span>
            {document.display_filename}
          </Link>
        ))}
      </section>

      <div className="flex flex-wrap gap-2" aria-label="Summary">
        {STATUS_ORDER.map((status) => (
          <span key={status} className={`rounded-full px-3 py-1 text-sm ${ITEM_STATUS_STYLES[status]}`}>
            {data.summary[status]} {status.toLowerCase()}
          </span>
        ))}
      </div>

      {groups.map((group) =>
        group.items.length === 0 ? null : (
          <section
            key={group.category}
            aria-label={group.category === "HEADER" ? "Header" : "Line items"}
            className="rounded-xl border border-slate-200 bg-white"
          >
            <h2 className="px-5 pt-4 font-medium">{group.category === "HEADER" ? "Header" : "Line items"}</h2>
            <table className="mt-2 w-full text-left text-sm">
              <thead className="border-b border-slate-200 text-xs uppercase text-slate-500">
                <tr>
                  <th className="py-2 pr-3 pl-5 font-medium">Item</th>
                  <th className="py-2 pr-3 font-medium">{leftRole}</th>
                  <th className="py-2 pr-3 font-medium">{rightRole}</th>
                  <th className="py-2 pr-3 font-medium">Result</th>
                  <th className="py-2 pr-5" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {group.items.map((item) => (
                  <ItemRow key={item.id} item={item} />
                ))}
              </tbody>
            </table>
          </section>
        ),
      )}
      <p className="text-xs text-slate-500">
        Tolerances: price ±{String(data.settings.price_abs)} or {Number(data.settings.price_pct) * 100}%, quantity ±
        {String(data.settings.quantity_abs)}. Differences involving a value read with less than{" "}
        {Math.round(Number(data.settings.min_confidence) * 100)}% confidence are marked uncertain.
      </p>
    </div>
  );
}
