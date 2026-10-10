import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { ApiToken, ApiTokenCreated } from "../lib/types";

// The tool permissions a token can carry (never more than the user's own).
const SCOPES: { scope: string; label: string }[] = [
  { scope: "documents:read", label: "Read documents, fields, evidence and rule results" },
  { scope: "knowledge:read", label: "Search the knowledge base" },
  { scope: "comparisons:create", label: "Compare documents" },
  { scope: "reviews:work", label: "Request reviews" },
];

function errorText(error: unknown): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : "The request failed.";
}

function tokenState(token: ApiToken): string {
  if (token.revoked_at) return "revoked";
  if (new Date(token.expires_at) <= new Date()) return "expired";
  return `expires ${new Date(token.expires_at).toLocaleDateString()}`;
}

export function ApiTokensPage() {
  const { token, user } = useAuth();
  const queryClient = useQueryClient();
  const available = SCOPES.filter((item) => user?.permissions.includes(item.scope));
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["documents:read"]);
  const [days, setDays] = useState(30);
  const [created, setCreated] = useState<ApiTokenCreated | null>(null);
  const tokens = useQuery({
    queryKey: ["api-tokens"],
    queryFn: () => apiRequest<ApiToken[]>("/api/v1/auth/tokens", { token }),
  });
  const create = useMutation({
    mutationFn: () =>
      apiRequest<ApiTokenCreated>("/api/v1/auth/tokens", {
        method: "POST",
        token,
        body: { name: name.trim(), scopes, expires_in_days: days },
      }),
    onSuccess: (result) => {
      setCreated(result);
      setName("");
      void queryClient.invalidateQueries({ queryKey: ["api-tokens"] });
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) => apiRequest<null>(`/api/v1/auth/tokens/${id}`, { method: "DELETE", token }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["api-tokens"] });
    },
  });
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (name.trim() && scopes.length > 0) create.mutate();
  };
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">API tokens</h1>
        <p className="mt-1 text-sm text-slate-600">
          Personal tokens let an MCP client (an IDE or desktop assistant) use the platform&apos;s tools as you: the same
          documents, the same permissions and an audit trail. A token is shown once; only its fingerprint is stored.
        </p>
      </div>
      <form onSubmit={submit} aria-label="New token" className="space-y-3 rounded-xl border border-slate-200 bg-white p-5">
        <label className="block text-sm">
          <span className="text-slate-700">Name</span>
          <input
            value={name}
            maxLength={100}
            onChange={(event) => {
              setName(event.target.value);
            }}
            placeholder="e.g. Laptop MCP client"
            className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          />
        </label>
        <fieldset className="text-sm">
          <legend className="text-slate-700">May be used to</legend>
          {available.map((item) => (
            <label key={item.scope} className="mt-1 flex items-center gap-2">
              <input
                type="checkbox"
                checked={scopes.includes(item.scope)}
                onChange={(event) => {
                  setScopes(
                    event.target.checked ? [...scopes, item.scope] : scopes.filter((scope) => scope !== item.scope),
                  );
                }}
              />
              {item.label}
            </label>
          ))}
        </fieldset>
        <label className="block text-sm">
          <span className="text-slate-700">Expires after (days)</span>
          <input
            type="number"
            min={1}
            max={90}
            value={days}
            onChange={(event) => {
              setDays(Number(event.target.value));
            }}
            className="ml-2 w-20 rounded-md border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        {create.error && (
          <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
            {errorText(create.error)}
          </p>
        )}
        <button
          type="submit"
          disabled={!name.trim() || scopes.length === 0 || create.isPending}
          className="rounded-md bg-blue-900 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800 disabled:opacity-50"
        >
          Create token
        </button>
      </form>
      {created && (
        <section aria-label="New token value" className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm">
          <p className="font-medium text-amber-900">Copy this token now — it will not be shown again.</p>
          <code className="mt-2 block break-all rounded bg-white px-2 py-1 font-mono text-xs">{created.token}</code>
          <p className="mt-2 text-xs text-amber-900">
            Use it as <code>MCP_API_TOKEN</code> for <code>docintel mcp</code> (stdio), or as a bearer token for the
            streamable HTTP endpoint.
          </p>
        </section>
      )}
      <section aria-label="Your tokens">
        <h2 className="text-sm font-semibold text-slate-700">Your tokens</h2>
        {tokens.data && tokens.data.length === 0 && <p className="mt-2 text-sm text-slate-500">No tokens.</p>}
        <ul className="mt-2 divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white text-sm">
          {tokens.data?.map((item) => (
            <li key={item.id} className="flex items-center justify-between gap-3 px-4 py-2">
              <div>
                <span className="font-medium">{item.name}</span>{" "}
                <span className="font-mono text-xs text-slate-500">{item.prefix}…</span>
                <div className="text-xs text-slate-500">
                  {item.scopes.join(", ")} · {tokenState(item)}
                  {item.last_used_at && ` · last used ${new Date(item.last_used_at).toLocaleString()}`}
                </div>
              </div>
              {!item.revoked_at && (
                <button
                  type="button"
                  onClick={() => {
                    revoke.mutate(item.id);
                  }}
                  className="rounded-md border border-slate-300 px-3 py-1 text-xs hover:bg-slate-100"
                >
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
