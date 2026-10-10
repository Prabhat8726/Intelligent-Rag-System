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
  /** Extra request headers (e.g. the session header of the cookie-authenticated auth calls). */
  headers?: Record<string, string>;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
  const isForm = options.body instanceof FormData;
  if (options.body !== undefined && !isForm) {
    headers["Content-Type"] = "application/json";
  }
  if (options.token) {
    headers.Authorization = `Bearer ${options.token}`;
  }

  const response = await fetch(path, {
    method: options.method ?? "GET",
    headers,
    // FormData sets its own multipart boundary header.
    body: options.body === undefined ? undefined : isForm ? (options.body as FormData) : JSON.stringify(options.body),
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

/** GET with the bearer token; non-2xx responses become ApiError (problem details if present). */
async function authorizedFetch(path: string, token: string | null, signal?: AbortSignal): Promise<Response> {
  const response = await fetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    credentials: "same-origin",
    signal,
  });
  if (!response.ok) {
    const contentType = response.headers.get("content-type") ?? "";
    const payload: unknown = contentType.includes("json") ? await response.json() : null;
    throw new ApiError(response.status, isProblemDetail(payload) ? payload : null);
  }
  return response;
}

/** Fetch an authenticated binary resource (e.g. a page preview image). */
export async function fetchBlob(path: string, token: string | null, signal?: AbortSignal): Promise<Blob> {
  return (await authorizedFetch(path, token, signal)).blob();
}

const BLOB_URL_LIFETIME_MS = 30_000;

/** Download an authenticated file: fetch with the bearer token, then save the blob. */
export async function downloadFile(path: string, token: string | null, fallbackName: string): Promise<void> {
  const response = await authorizedFetch(path, token);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filenameFromDisposition(response.headers.get("content-disposition")) ?? fallbackName;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  // Some browsers start the download asynchronously; revoking immediately can cancel it.
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, BLOB_URL_LIFETIME_MS);
}

export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded?.[1]) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch {
      // fall through to the ASCII fallback
    }
  }
  const plain = /filename="([^"]+)"/i.exec(header);
  return plain?.[1] ?? null;
}
