import { useQuery } from "@tanstack/react-query";

import { useAuth } from "../auth/useAuth";
import { apiRequest } from "../lib/api";
import type { ReadinessResponse } from "../lib/types";

const REFRESH_INTERVAL_MS = 15_000;

function StatusBadge({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
        ok ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"
      }`}
    >
      {label}
    </span>
  );
}

export function SystemStatusPage() {
  const { user } = useAuth();
  const readiness = useQuery({
    queryKey: ["readiness"],
    // 503 carries a valid body describing which check failed.
    queryFn: ({ signal }) =>
      apiRequest<ReadinessResponse>("/health/ready", { signal, acceptStatuses: [503] }),
    refetchInterval: REFRESH_INTERVAL_MS,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">System status</h1>
        <p className="mt-1 text-sm text-slate-500">
          Live readiness of the platform services. Refreshes every 15 seconds.
        </p>
      </div>

      <section aria-labelledby="readiness-heading" className="rounded-xl border border-slate-200 bg-white p-6">
        <div className="flex items-center justify-between">
          <h2 id="readiness-heading" className="font-medium">
            Backend readiness
          </h2>
          {readiness.data && (
            <StatusBadge
              ok={readiness.data.status === "ready"}
              label={readiness.data.status === "ready" ? "Ready" : "Not ready"}
            />
          )}
        </div>
        {readiness.isPending && (
          <p role="status" className="mt-4 text-sm text-slate-500">
            Checking…
          </p>
        )}
        {readiness.isError && (
          <p role="alert" className="mt-4 text-sm text-red-700">
            The API could not be reached.
          </p>
        )}
        {readiness.data && (
          <>
            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-slate-500">
                  <tr>
                    <th className="py-2 font-medium">Check</th>
                    <th className="py-2 font-medium">Status</th>
                    <th className="py-2 font-medium">Detail</th>
                    <th className="py-2 text-right font-medium">Latency</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {Object.entries(readiness.data.checks).map(([name, check]) => (
                    <tr key={name}>
                      <td className="py-2 capitalize">{name}</td>
                      <td className="py-2">
                        <StatusBadge ok={check.status === "ok"} label={check.status === "ok" ? "OK" : "Failing"} />
                      </td>
                      <td className="py-2 text-slate-600">{check.detail ?? "—"}</td>
                      <td className="py-2 text-right tabular-nums text-slate-600">
                        {check.latency_ms === null ? "—" : `${check.latency_ms.toFixed(1)} ms`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-4 text-xs text-slate-500">API version {readiness.data.version}</p>
          </>
        )}
      </section>

      {user && (
        <section aria-labelledby="access-heading" className="rounded-xl border border-slate-200 bg-white p-6">
          <h2 id="access-heading" className="font-medium">
            Your access
          </h2>
          <dl className="mt-4 grid grid-cols-1 gap-4 text-sm sm:grid-cols-3">
            <div>
              <dt className="text-slate-500">Role</dt>
              <dd className="font-medium">{user.role}</dd>
            </div>
            <div>
              <dt className="text-slate-500">Department</dt>
              <dd className="font-medium">{user.department?.name ?? "All (organization-wide)"}</dd>
            </div>
            <div>
              <dt className="text-slate-500">Email</dt>
              <dd className="font-medium">{user.email}</dd>
            </div>
          </dl>
          <h3 className="mt-6 text-sm text-slate-500">Permissions</h3>
          <ul aria-label="Permissions" className="mt-2 flex flex-wrap gap-2">
            {user.permissions.map((permission) => (
              <li key={permission} className="rounded-md bg-slate-100 px-2 py-1 font-mono text-xs text-slate-700">
                {permission}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
