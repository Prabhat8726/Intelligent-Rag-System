/** Response shapes mirrored from the backend OpenAPI schema (docintel.api.schemas). */

export type Role = "ADMIN" | "MANAGER" | "ANALYST" | "REVIEWER" | "VIEWER";

export interface Department {
  id: string;
  name: string;
}

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  department: Department | null;
  is_active: boolean;
  last_login_at: string | null;
}

export interface CurrentUser extends User {
  permissions: string[];
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user: User;
}

export interface CheckResult {
  status: "ok" | "fail";
  detail: string | null;
  latency_ms: number | null;
}

export interface ReadinessResponse {
  status: "ready" | "not_ready";
  version: string;
  checks: Record<string, CheckResult>;
}
