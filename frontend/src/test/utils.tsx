import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { vi } from "vitest";

import { AuthProvider } from "../auth/AuthProvider";
import { routes } from "../routes";

type Handler = (url: string, init: RequestInit | undefined) => Response | Promise<Response>;

export function jsonResponse(body: unknown, status = 200, contentType = "application/json"): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": contentType },
  });
}

export function problem(status: number, detail: string): Response {
  return jsonResponse(
    {
      type: "about:blank",
      title: "Error",
      status,
      detail,
      request_id: "req-1",
    },
    status,
    "application/problem+json",
  );
}

// The browser's session cookie (httpOnly, so the app never sees it): set by `browserHasSession`.
let sessionCookie = false;

/** Start the test with a session cookie: the app restores the session via /auth/refresh. */
export function browserHasSession(): void {
  sessionCookie = true;
}

export function resetBrowserSession(): void {
  sessionCookie = false;
}

const SESSION_HANDLERS: Record<string, Handler> = {
  "/api/v1/auth/refresh": () =>
    sessionCookie ? jsonResponse(TOKEN_RESPONSE) : problem(401, "Your session has ended. Sign in again."),
  "/api/v1/auth/logout": () => {
    sessionCookie = false;
    return new Response(null, { status: 204 });
  },
};

/** Replace global fetch with a router over request URLs. Unmatched URLs fail the test loudly.
 *  The session endpoints answer from the simulated cookie unless a test overrides them. */
export function mockFetch(handlers: Record<string, Handler>) {
  const routes = { ...SESSION_HANDLERS, ...handlers };
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const handler = routes[url];
    if (!handler) {
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    }
    return Promise.resolve(handler(url, init));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

export function renderApp(initialPath: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const router = createMemoryRouter(routes, { initialEntries: [initialPath] });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return { router };
}

export const CURRENT_USER = {
  id: "8f5c2a64-2a3c-4a8e-9a7d-2f4b8c9d0e11",
  email: "analyst@docintel.local",
  full_name: "Finance Analyst",
  role: "ANALYST",
  department: { id: "0b6f5c2e-1111-4a8e-9a7d-2f4b8c9d0e12", name: "Finance" },
  is_active: true,
  last_login_at: null,
  permissions: ["documents:read", "documents:upload"],
};

export const TOKEN_RESPONSE = {
  access_token: "header.payload.signature",
  token_type: "bearer",
  expires_in: 900,
  user: CURRENT_USER,
};
