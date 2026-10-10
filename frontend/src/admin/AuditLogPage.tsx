import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { AuditEventPage } from "../lib/types";

// Prefix filters (the API matches a value ending in "." as a prefix).
const ACTION_GROUPS: [string, string][] = [
  ["", "All events"],
  ["workflow.", "Workflows and approvals"],
  ["review_task.", "Review decisions"],
  ["review.", "Review requests"],
  ["document.", "Documents"],
  ["report.", "Reports"],
  ["analysis.", "AI analysis"],
  ["auth.", "Sign-ins"],
  ["user.", "Users"],
  ["authz.denied", "Permission denied"],
];

export function AuditLogPage() {
  const { token, user } = useAuth();
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState("");
  const events = useInfiniteQuery({
    queryKey: ["audit-logs", action, outcome],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: "50" });
      if (action) params.set("action", action);
      if (outcome) params.set("outcome", outcome);
      if (pageParam !== null) params.set("before_id", String(pageParam));
      return apiRequest<AuditEventPage>(`/api/v1/audit-logs?${params.toString()}`, { token });
    },
    getNextPageParam: (page) => page.next_before_id,
  });
  const items = events.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Audit log</h1>
        <p className="mt-1 text-sm text-slate-600">
          Every sign-in, upload, decision, approval and change, newest first. The log is append-only: the database
          rejects edits and deletions.{" "}
          {user?.role === "ADMIN"
            ? "You see every event."
            : "You see the events of your department: caused by its members or about its documents."}
        </p>
      </div>
      <div className="flex flex-wrap gap-3">
        <select
          aria-label="Event type"
          value={action}
          onChange={(event) => {
            setAction(event.target.value);
          }}
          className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          {ACTION_GROUPS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <select
          aria-label="Outcome"
          value={outcome}
          onChange={(event) => {
            setOutcome(event.target.value);
          }}
          className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          <option value="">Any outcome</option>
          <option value="SUCCESS">Success</option>
          <option value="FAILURE">Failure</option>
          <option value="DENIED">Denied</option>
        </select>
      </div>
      <section aria-label="Audit events" className="rounded-xl border border-slate-200 bg-white">
        {events.isPending ? (
          <p className="p-5 text-sm text-slate-500">Loading…</p>
        ) : events.isError ? (
          <p role="alert" className="p-5 text-sm text-red-700">
            {events.error instanceof ApiError
              ? (events.error.problem?.detail ?? events.error.message)
              : "The audit log could not be loaded."}
          </p>
        ) : items.length === 0 ? (
          <p className="p-5 text-sm text-slate-500">No events.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-200 uppercase text-slate-500">
                <tr>
                  <th className="px-4 py-2 font-medium">When</th>
                  <th className="py-2 font-medium">Event</th>
                  <th className="py-2 font-medium">Who</th>
                  <th className="py-2 font-medium">About</th>
                  <th className="py-2 font-medium">Outcome</th>
                  <th className="py-2 pr-4 font-medium">Details</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {items.map((item) => (
                  <tr key={item.id} className="align-top">
                    <td className="whitespace-nowrap px-4 py-2">{new Date(item.occurred_at).toLocaleString()}</td>
                    <td className="py-2 font-mono">{item.action}</td>
                    <td className="py-2">
                      {item.actor ? item.actor.email : item.actor_type.toLowerCase()}
                      {item.actor && item.actor_type !== "USER" && (
                        <span className="block text-slate-500">via {item.actor_type.toLowerCase()}</span>
                      )}
                      {item.ip_address && <span className="block text-slate-500">{item.ip_address}</span>}
                    </td>
                    <td className="py-2">
                      {item.entity_type ? `${item.entity_type} ${item.entity_id?.slice(0, 8) ?? ""}` : "—"}
                    </td>
                    <td className={`py-2 ${item.outcome === "SUCCESS" ? "text-emerald-700" : "text-red-700"}`}>
                      {item.outcome.toLowerCase()}
                    </td>
                    <td className="max-w-sm break-all py-2 pr-4 font-mono text-slate-600">
                      {Object.keys(item.details).length > 0 ? JSON.stringify(item.details) : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {events.hasNextPage && (
        <button
          type="button"
          disabled={events.isFetchingNextPage}
          onClick={() => {
            void events.fetchNextPage();
          }}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-50"
        >
          Older events
        </button>
      )}
    </div>
  );
}
