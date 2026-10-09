import { useMutation } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth/useAuth";
import { DOCUMENT_TYPE_LABELS } from "../documents/format";
import { ApiError, apiRequest } from "../lib/api";
import type { DocumentSearchResponse, SearchHit } from "../lib/types";

const EXAMPLES = [
  "invoices from Kestrel Industrial Supply",
  "documents with payment terms longer than 45 days",
  "purchase orders over 2,000",
  "contracts containing termination clauses",
];

function Hit({ hit }: { hit: SearchHit }) {
  const type = hit.document.document_type;
  const facts = [
    type ? DOCUMENT_TYPE_LABELS[type] : null,
    hit.vendor_name,
    hit.document_date,
    hit.total ? `total ${hit.total}` : null,
    hit.payment_terms_days !== null ? `payment terms ${hit.payment_terms_days} days` : null,
  ].filter(Boolean);
  return (
    <li className="rounded-lg border border-slate-200 bg-white p-4">
      <Link to={`/documents/${hit.document.id}`} className="font-medium text-blue-900 hover:underline">
        {hit.document.display_filename}
      </Link>
      <p className="mt-0.5 text-sm text-slate-600">{facts.join(" · ")}</p>
      {hit.snippet && (
        <p className="mt-2 line-clamp-3 whitespace-pre-wrap border-l-2 border-slate-200 pl-3 text-sm text-slate-800">
          {hit.snippet.text}
          {hit.snippet.page_start !== null && <span className="text-xs text-slate-500"> (page {hit.snippet.page_start})</span>}
        </p>
      )}
      {hit.reasons.length > 0 && (
        <p className="mt-2 text-xs text-slate-500">Matched: {hit.reasons.join("; ")}</p>
      )}
    </li>
  );
}

export function SearchPage() {
  const { token } = useAuth();
  const [query, setQuery] = useState("");
  const search = useMutation({
    mutationFn: (text: string) =>
      apiRequest<DocumentSearchResponse>("/api/v1/search", { method: "POST", token, body: { query: text } }),
  });
  const run = (text: string) => {
    if (text.trim()) search.mutate(text.trim());
  };
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    run(query);
  };
  const error =
    search.error instanceof ApiError
      ? (search.error.problem?.detail ?? search.error.message)
      : search.error
        ? "The search failed."
        : null;
  const result = search.data;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Search documents</h1>
        <p className="mt-1 text-sm text-slate-600">
          Ask in plain language: types, vendors, payment terms, totals and dates become filters on the extracted
          data; anything else is searched in the documents&apos; text.
        </p>
      </div>
      <form onSubmit={submit} aria-label="Search documents" className="flex gap-2">
        <input
          aria-label="Search"
          value={query}
          maxLength={500}
          placeholder="e.g. invoices from Bluepeak Office Solutions in 2026"
          onChange={(event) => {
            setQuery(event.target.value);
          }}
          className="flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={!query.trim() || search.isPending}
          className="rounded-md bg-blue-900 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800 disabled:opacity-50"
        >
          Search
        </button>
      </form>
      <div className="flex flex-wrap gap-2 text-xs">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => {
              setQuery(example);
              run(example);
            }}
            className="rounded-full border border-slate-300 px-3 py-1 text-slate-700 hover:bg-slate-100"
          >
            {example}
          </button>
        ))}
      </div>
      {error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}
      {result && (
        <section aria-label="Results" className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-xs" aria-label="Interpretation">
            <span className="text-slate-500">Understood as:</span>
            {result.interpretation.recognized.map((item) => (
              <span key={item} className="rounded bg-blue-50 px-2 py-0.5 text-blue-900">
                {item}
              </span>
            ))}
            {result.interpretation.text && (
              <span className="rounded bg-slate-100 px-2 py-0.5 text-slate-700">
                text “{result.interpretation.text}”
              </span>
            )}
            {result.interpretation.vendor && result.interpretation.vendors_matched.length === 0 && (
              <span className="text-amber-800">no vendor master entry matched; printed names are searched</span>
            )}
          </div>
          <p className="text-sm text-slate-600">
            {result.total === 0
              ? "No documents found."
              : `${result.total} document${result.total === 1 ? "" : "s"}${
                  result.results.length < result.total ? ` (first ${result.results.length})` : ""
                }`}
          </p>
          <ul className="space-y-2">
            {result.results.map((hit) => (
              <Hit key={hit.document.id} hit={hit} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
