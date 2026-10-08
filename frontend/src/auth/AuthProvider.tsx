import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { ApiError, apiRequest } from "../lib/api";
import type { CurrentUser, TokenResponse } from "../lib/types";
import { AuthContext, type AuthContextValue, type AuthStatus } from "./authContext";
import { clearSession, loadSession, saveSession, type StoredSession } from "./session";

const ME_QUERY_KEY = "current-user";

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [session, setSession] = useState<StoredSession | null>(() => loadSession());

  const logout = useCallback(() => {
    clearSession();
    setSession(null);
    queryClient.removeQueries({ queryKey: [ME_QUERY_KEY] });
  }, [queryClient]);

  const meQuery = useQuery({
    queryKey: [ME_QUERY_KEY, session?.token],
    queryFn: ({ signal }) =>
      apiRequest<CurrentUser>("/api/v1/auth/me", { token: session?.token, signal }),
    enabled: session !== null,
    retry: false,
    staleTime: 60_000,
  });

  // A rejected token (expired, user deactivated) means the session is over: the user is treated
  // as anonymous immediately, and the stale token is removed from storage.
  const tokenRejected = meQuery.error instanceof ApiError && meQuery.error.status === 401;
  useEffect(() => {
    if (tokenRejected) clearSession();
  }, [tokenRejected]);

  // Sign out exactly when the access token expires.
  useEffect(() => {
    if (session === null) return;
    const timer = window.setTimeout(logout, Math.max(0, session.expiresAt - Date.now()));
    return () => {
      window.clearTimeout(timer);
    };
  }, [session, logout]);

  const login = useCallback(async (email: string, password: string) => {
    const response = await apiRequest<TokenResponse>("/api/v1/auth/login", {
      method: "POST",
      body: { email, password },
    });
    const next: StoredSession = {
      token: response.access_token,
      expiresAt: Date.now() + response.expires_in * 1000,
    };
    saveSession(next);
    setSession(next);
  }, []);

  const { refetch } = meQuery;
  const retry = useCallback(() => {
    void refetch();
  }, [refetch]);

  let status: AuthStatus = "loading";
  if (session === null || tokenRejected) status = "anonymous";
  else if (meQuery.data) status = "authenticated";
  else if (meQuery.isError) status = "error";

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      token: tokenRejected ? null : (session?.token ?? null),
      user: meQuery.data ?? null,
      login,
      logout,
      retry,
    }),
    [status, tokenRejected, session, meQuery.data, login, logout, retry],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}
