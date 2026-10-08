/**
 * Minimal typed client for the backend. Errors are RFC 9457 problem details, surfaced as ApiError.
 * Requests are same-origin (/api is proxied by Vite in dev and nginx in containers).
 */

export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance?: string | null;
  request_id?: string | null;
  errors?: { field: string; message: string; type: string }[];
}

export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetail | null;

  constructor(status: number, problem: ProblemDetail | null) {
    super(problem?.detail ?? `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }
}

function isProblemDetail(value: unknown): value is ProblemDetail {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    "detail" in value &&
    typeof (value as { detail: unknown }).detail === "string"
  );
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  token?: string | null;
  signal?: AbortSignal;
  /** HTTP statuses whose JSON body is a valid result rather than an error (e.g. 503 readiness). */
  acceptStatuses?: number[];
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (options.token) {
    headers.Authorization = `Bearer ${options.token}`;
  }

  const response = await fetch(path, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    credentials: "same-origin",
    signal: options.signal,
  });

  const contentType = response.headers.get("content-type") ?? "";
  const payload: unknown = contentType.includes("json") ? await response.json() : null;

  if (!response.ok && !options.acceptStatuses?.includes(response.status)) {
    throw new ApiError(response.status, isProblemDetail(payload) ? payload : null);
  }
  return payload as T;
}
