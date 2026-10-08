import { createContext } from "react";

import type { CurrentUser } from "../lib/types";

export type AuthStatus = "loading" | "authenticated" | "anonymous" | "error";

export interface AuthContextValue {
  status: AuthStatus;
  token: string | null;
  user: CurrentUser | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  /** Re-fetch the current user after a transient (non-401) failure. */
  retry: () => void;
}

export const AuthContext = createContext<AuthContextValue | null>(null);
