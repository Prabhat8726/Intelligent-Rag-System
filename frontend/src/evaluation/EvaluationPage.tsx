import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { EvaluationPage as EvaluationList, EvaluationSummary } from "../lib/types";
import { formatDate, shortRevision } from "./format";
import { GateBadge } from "./GateBadge";

function SuiteCard({ run }: { run: EvaluationSummary }) {
  const headingId = `suite-${run.suite}`;
  return (
    <article aria-labelledby={headingId} className="rounded-xl border border-slate-200 bg-white p-5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h2 id={headingId} className="font-medium">
          <Link to={`/evaluation/${run.id}`} className="text-blue-900 hover:underline">
            {run.title}
          </Link>
        </h2>
        <GateBadge gates={run.gates} />
      </div>
      <p className="mt-1 text-xs text-slate-500">
        <span className="font-mono">{run.suite}</span> · commit{" "}
        <span className="font-mono">{shortRevision(run.git_revision)}</span> · run {formatDate(run.run_at)}
      </p>
      {run.headlines.length > 0 && (
        <dl className="mt-3 space-y-2 text-sm">
          {run.headlines.map((headline) => (
            <div key={headline.label}>
              <dt className="text-slate-600">{headline.label}</dt>
              <dd className="font-medium text-slate-900">
                {headline.value ?? <span className="font-normal text-slate-500">Not in this run</span>}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </article>
  );
}

export function EvaluationPage() {
  const { token } = useAuth();
  const runs = useQuery({
    queryKey: ["evaluations", "latest"],
    queryFn: () => apiRequest<EvaluationList>("/api/v1/evaluations?latest=true&limit=100", { token }),
  });
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Evaluation</h1>
        <p className="mt-1 max-w-3xl text-sm text-slate-600">
          What the platform achieves, measured: the latest full run of every evaluation suite, the regression gates it
          passed and its headline numbers. Every number comes from a recorded run of <code>make evaluate</code> against
          generator ground truth, on <strong>synthetic documents only</strong>, so real documents will do worse.
        </p>
      </div>
      {runs.isPending ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : runs.isError ? (
        <p role="alert" className="text-sm text-red-700">
          {runs.error instanceof ApiError
            ? (runs.error.problem?.detail ?? runs.error.message)
            : "The evaluation results could not be loaded."}
        </p>
      ) : runs.data.items.length === 0 ? (
        <p className="rounded-xl border border-slate-200 bg-white p-5 text-sm text-slate-600">
          No evaluation results are recorded yet. <code>make seed</code> (or <code>make seed-docker</code>) loads the
          committed reports; <code>docintel evaluate --record</code> records a new run.
        </p>
      ) : (
        <section aria-label="Evaluation suites" className="grid gap-4 lg:grid-cols-2">
          {runs.data.items.map((run) => (
            <SuiteCard key={run.id} run={run} />
          ))}
        </section>
      )}
    </div>
  );
}
