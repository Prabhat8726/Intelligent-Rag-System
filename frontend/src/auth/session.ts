/**
 * Access-token storage. Phase 0 keeps the short-lived token in sessionStorage (tab-scoped);
 * Phase 9 moves to an in-memory token plus an httpOnly refresh cookie (ADR-010).
 */

const STORAGE_KEY = "docintel.session";

export interface StoredSession {
  token: string;
  expiresAt: number; // epoch milliseconds
}

function isStoredSession(value: unknown): value is StoredSession {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as StoredSession).token === "string" &&
    typeof (value as StoredSession).expiresAt === "number"
  );
}

export function loadSession(now: number = Date.now()): StoredSession | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!isStoredSession(parsed) || parsed.expiresAt <= now) {
      window.sessionStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function saveSession(session: StoredSession): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
  } catch {
    // Storage can be unavailable (private mode); the session then lives only in memory.
  }
}

export function clearSession(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
