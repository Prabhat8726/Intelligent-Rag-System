import { useMutation } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { AnswerSource, KnowledgeAnswer, KnowledgeCategory } from "../lib/types";
import { ANSWER_STATUS, CATEGORIES, CATEGORY_LABELS, formatPeriod } from "./format";

function SourceCard({ source }: { source: AnswerSource }) {
  const [open, setOpen] = useState(source.cited);
  const period = formatPeriod(source.effective_from, source.effective_to);
  const pages =
    source.page_start === null
      ? ""
      : source.page_start === source.page_end
        ? `p. ${source.page_start}`
        : `pp. ${source.page_start}–${source.page_end ?? source.page_start}`;
  return (
    <li id={`source-${source.label}`} className="rounded-lg border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap items-baseline gap-2 text-sm">
        <span className="rounded bg-blue-900 px-1.5 py-0.5 font-mono text-xs text-white">{source.label}</span>
        <Link to={`/knowledge/${source.knowledge_document_id}`} className="font-medium text-blue-900 hover:underline">
          {source.title}
        </Link>
        {source.version_label && <span className="text-slate-500">v{source.version_label}</span>}
        {source.status === "SUPERSEDED" && (
          <span className="rounded bg-slate-100 px-1.5 text-xs text-slate-700">earlier version</span>
        )}
        {source.cited && <span className="rounded bg-emerald-50 px-1.5 text-xs text-emerald-800">cited</span>}
        {!source.sent_to_model && (
          <span
            className="rounded bg-amber-50 px-1.5 text-xs text-amber-800"
            title="Not sent to the language model (no model, or above the external AI sensitivity limit)"
          >
            not sent to model
          </span>
        )}
      </div>
      <p className="mt-1 text-xs text-slate-500">
        {[source.section_path, pages, period && `in force ${period}`].filter(Boolean).join(" · ")}
      </p>
      <button
        type="button"
        onClick={() => {
          setOpen(!open);
        }}
        className="mt-1 text-xs text-blue-900 hover:underline"
        aria-expanded={open}
      >
        {open ? "Hide passage" : "Show passage"}
      </button>
      {open && <p className="mt-2 whitespace-pre-wrap text-sm text-slate-800">{source.content}</p>}
    </li>
  );
}

export function AnswerView({ answer }: { answer: KnowledgeAnswer }) {
  const status = ANSWER_STATUS[answer.status];
  return (
    <section aria-label="Answer" className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <span className={`rounded-md px-2 py-1 text-sm font-medium ${status.style}`}>{status.label}</span>
        <span className="text-sm text-slate-600">{status.help}</span>
      </div>
      {answer.claims.length > 0 && (
        <ul className="space-y-2" aria-label="Statements">
          {answer.claims.map((claim, index) => (
            <li key={index} className="text-slate-900">
              <span className={claim.grounded ? "" : "bg-amber-50"}>{claim.text}</span>{" "}
              {claim.citations.map((label) => (
                <a
                  key={label}
                  href={`#source-${label}`}
                  className="ml-1 rounded bg-blue-50 px-1 font-mono text-xs text-blue-900 hover:bg-blue-100"
                  aria-label={`Source ${label}`}
                >
                  {label}
                </a>
              ))}
              {!claim.grounded && (
                <span className="ml-2 text-xs text-amber-800">check against the source</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {answer.notices.length > 0 && (
        <ul className="space-y-1 text-sm text-slate-600" aria-label="Notices">
          {answer.notices.map((notice) => (
            <li key={notice}>• {notice}</li>
          ))}
        </ul>
      )}
      {answer.sources.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-slate-700">
            {answer.status === "INSUFFICIENT_EVIDENCE" ? "Closest passages" : "Sources"}
          </h3>
          <ol className="mt-2 space-y-2">
            {answer.sources.map((source) => (
              <SourceCard key={source.label} source={source} />
            ))}
          </ol>
        </div>
      )}
      <p className="text-xs text-slate-500">
        {answer.retrieval.mode === "hybrid" ? "Hybrid search (vectors + full text)" : "Full-text search"} · versions
        in force on {answer.retrieval.as_of} · term coverage {Math.round(answer.evidence.term_coverage * 100)}%
        {answer.model && ` · ${answer.model}`}
      </p>
    </section>
  );
}

export function AskPanel() {
  const { token } = useAuth();
  const [question, setQuestion] = useState("");
  const [asOf, setAsOf] = useState("");
  const [category, setCategory] = useState<KnowledgeCategory | "">("");
  const ask = useMutation({
    mutationFn: () =>
      apiRequest<KnowledgeAnswer>("/api/v1/knowledge/query", {
        method: "POST",
        token,
        body: {
          question: question.trim(),
          ...(asOf ? { as_of: asOf } : {}),
          ...(category ? { categories: [category] } : {}),
        },
      }),
  });
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (question.trim().length >= 3) ask.mutate();
  };
  const error =
    ask.error instanceof ApiError ? (ask.error.problem?.detail ?? ask.error.message) : ask.error ? "The request failed." : null;
  return (
    <div className="space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <form onSubmit={submit} aria-label="Ask the knowledge base" className="space-y-3">
        <label htmlFor="knowledge-question" className="block text-sm font-medium text-slate-700">
          Ask about policies, procedures and guidelines
        </label>
        <textarea
          id="knowledge-question"
          value={question}
          maxLength={1000}
          rows={2}
          placeholder="e.g. Who must approve payment terms longer than 60 days?"
          onChange={(event) => {
            setQuestion(event.target.value);
          }}
          className="block w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs">
            <span className="block text-slate-500">Category</span>
            <select
              value={category}
              onChange={(event) => {
                setCategory(event.target.value as KnowledgeCategory | "");
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
          <label className="text-xs">
            <span className="block text-slate-500">Versions in force on</span>
            <input
              type="date"
              value={asOf}
              onChange={(event) => {
                setAsOf(event.target.value);
              }}
              className="mt-1 rounded-md border border-slate-300 px-2 py-1 text-sm"
            />
          </label>
          <button
            type="submit"
            disabled={question.trim().length < 3 || ask.isPending}
            className="rounded-md bg-blue-900 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800 disabled:opacity-50"
          >
            {ask.isPending ? "Searching…" : "Ask"}
          </button>
        </div>
      </form>
      {error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}
      {ask.data && <AnswerView answer={ask.data} />}
    </div>
  );
}
