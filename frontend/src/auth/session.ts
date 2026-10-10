/**
 * Browser session (ADR-063): the access token lives in memory only; an httpOnly refresh cookie
 * (scoped to /api/v1/auth, invisible to scripts) restores and renews it.
 *
 * Refreshing rotates the cookie and a cookie works once, so two concurrent refreshes would look
 * like a stolen cookie and end the session: refreshes are single-flight within a tab and
 * serialized across tabs with the Web Locks API.
 */

import { ApiError, apiRequest } from "../lib/api";
import type { TokenResponse } from "../lib/types";

/** Sent on cookie-authenticated calls; a cross-site page cannot add it (CSRF defence). */
export const SESSION_HEADER = { "X-Docintel-Session": "1" } as const;
const LOCK_NAME = "docintel-session-refresh";
const LEGACY_STORAGE_KEY = "docintel.session"; // Phase 0-8 kept the token in sessionStorage

export interface Session {
  token: string;
  expiresAt: number; // epoch milliseconds
  userId: string;
}

export function sessionFrom(response: TokenResponse, now: number = Date.now()): Session {
  return {
    token: response.access_token,
    expiresAt: now + response.expires_in * 1000,
    userId: response.user.id,
  };
}

/** When to renew: a minute before expiry, or at 80% of a short lifetime. */
export function refreshDelay(session: Session, now: number = Date.now()): number {
  const remaining = session.expiresAt - now;
  return Math.max(1_000, remaining - 60_000, remaining * 0.8);
}

function withLock<T>(task: () => Promise<T>): Promise<T> {
  const locks = typeof navigator !== "undefined" && "locks" in navigator ? navigator.locks : null;
  return locks ? locks.request(LOCK_NAME, task) : task();
}

let inflight: Promise<Session | null> | null = null;

/** A new access token from the session cookie; null when there is no session (any longer). */
export function refreshSession(): Promise<Session | null> {
  inflight ??= withLock(async () => {
    try {
      const response = await apiRequest<TokenResponse>("/api/v1/auth/refresh", {
        method: "POST",
        headers: SESSION_HEADER,
      });
      return sessionFrom(response);
    } catch (error) {
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) return null;
      throw error;
    }
  }).finally(() => {
    inflight = null;
  });
  return inflight;
}

export async function signIn(email: string, password: string): Promise<Session> {
  const response = await apiRequest<TokenResponse>("/api/v1/auth/login", {
    method: "POST",
    body: { email, password },
    headers: SESSION_HEADER,
  });
  return sessionFrom(response);
}

/** Revoke the session cookie (best effort: the local session ends either way). */
export async function signOut(): Promise<void> {
  try {
    await apiRequest<unknown>("/api/v1/auth/logout", {
      method: "POST",
      headers: SESSION_HEADER,
    });
  } catch {
    // Offline or already signed out: nothing to revoke from here.
  }
}

/** Remove a token an earlier version stored in sessionStorage. */
export function forgetLegacySession(): void {
  try {
    window.sessionStorage.removeItem(LEGACY_STORAGE_KEY);
  } catch {
    // Storage unavailable: nothing was stored.
  }
}
