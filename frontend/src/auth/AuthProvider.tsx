import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError, apiRequest } from "../lib/api";
import type { CurrentUser } from "../lib/types";
import { AuthContext, type AuthContextValue, type AuthStatus } from "./authContext";
import { forgetLegacySession, refreshDelay, refreshSession, signIn, signOut, type Session } from "./session";

const ME_QUERY_KEY = "current-user";
const CHANNEL = "docintel-auth";

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [session, setSession] = useState<Session | null>(null);
  const [restoring, setRestoring] = useState(true);
  const channel = useRef<BroadcastChannel | null>(null);
  // Renewals since /me last succeeded: one try per rejection, never a loop.
  const renewals = useRef(0);

  const endLocally = useCallback(() => {
    setSession(null);
    queryClient.removeQueries({ queryKey: [ME_QUERY_KEY] });
  }, [queryClient]);

  // Restore the session from the cookie once; other tabs announce their sign-outs.
  useEffect(() => {
    forgetLegacySession();
    let active = true;
    refreshSession()
      .then((restored) => {
        if (active) setSession(restored);
      })
      .catch(() => {
        if (active) setSession(null);
      })
      .finally(() => {
        if (active) setRestoring(false);
      });
    if (typeof BroadcastChannel !== "undefined") {
      channel.current = new BroadcastChannel(CHANNEL);
      channel.current.onmessage = (event: MessageEvent) => {
        if (event.data === "signed-out") endLocally();
      };
    }
    return () => {
      active = false;
      channel.current?.close();
      channel.current = null;
    };
  }, [endLocally]);

  const renew = useCallback(async () => {
    try {
      const next = await refreshSession();
      if (next === null) endLocally();
      else setSession(next);
    } catch {
      // A network error: keep the current token; the next request or timer tries again.
    }
  }, [endLocally]);

  // Renew the access token shortly before it expires (the cookie rotates each time).
  useEffect(() => {
    if (session === null) return;
    const timer = window.setTimeout(() => {
      void renew();
    }, refreshDelay(session));
    return () => {
      window.clearTimeout(timer);
    };
  }, [session, renew]);

  const meQuery = useQuery({
    queryKey: [ME_QUERY_KEY, session?.token],
    queryFn: ({ signal }) =>
      apiRequest<CurrentUser>("/api/v1/auth/me", {
        token: session?.token,
        signal,
      }),
    enabled: session !== null,
    retry: false,
    staleTime: 60_000,
    // A renewed token keeps showing the signed-in user while /me is fetched again (never
    // another user's data after signing in as someone else).
    placeholderData: (previous) => (previous && previous.id === session?.userId ? previous : undefined),
  });

  // A rejected token (expired while the computer slept, user deactivated): try the cookie once.
  const tokenRejected = meQuery.error instanceof ApiError && meQuery.error.status === 401;
  const meLoaded = meQuery.isSuccess && !meQuery.isPlaceholderData;
  useEffect(() => {
    if (meLoaded) renewals.current = 0;
  }, [meLoaded, session]);
  useEffect(() => {
    if (!tokenRejected || !session) return;
    if (renewals.current >= 1) {
      endLocally();
      return;
    }
    renewals.current += 1;
    void renew();
  }, [tokenRejected, session, renew, endLocally]);

  const login = useCallback(async (email: string, password: string) => {
    setSession(await signIn(email, password));
  }, []);

  const logout = useCallback(() => {
    endLocally();
    channel.current?.postMessage("signed-out");
    void signOut();
  }, [endLocally]);

  const { refetch } = meQuery;
  const retry = useCallback(() => {
    void refetch();
  }, [refetch]);

  let status: AuthStatus = "loading";
  if (restoring) status = "loading";
  else if (session === null) status = "anonymous";
  else if (meQuery.data) status = "authenticated";
  else if (meQuery.isError && !tokenRejected) status = "error";

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      token: session?.token ?? null,
      user: meQuery.data ?? null,
      login,
      logout,
      retry,
    }),
    [status, session, meQuery.data, login, logout, retry],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}
