import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { refreshDelay } from "./auth/session";
import {
  browserHasSession,
  CURRENT_USER,
  jsonResponse,
  mockFetch,
  problem,
  renderApp,
  TOKEN_RESPONSE,
} from "./test/utils";

const READY = {
  status: "ready",
  version: "0.1.0",
  checks: {
    database: { status: "ok", detail: null, latency_ms: 4.2 },
    migrations: { status: "ok", detail: null, latency_ms: null },
  },
};

function signedIn() {
  browserHasSession();
}

function calls(fetchMock: ReturnType<typeof mockFetch>, url: string) {
  return fetchMock.mock.calls.filter(([called]) => called === url);
}

describe("authentication flow", () => {
  it("redirects anonymous users to the login page", async () => {
    const fetchMock = mockFetch({});
    const { router } = renderApp("/status");

    expect(await screen.findByRole("heading", { name: "Document Intelligence" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    // One attempt to restore a session from the cookie, then nothing.
    expect(calls(fetchMock, "/api/v1/auth/refresh")).toHaveLength(1);
  });

  it("signs in and lands on the requested page", async () => {
    const fetchMock = mockFetch({
      "/api/v1/auth/login": () => jsonResponse(TOKEN_RESPONSE),
      "/api/v1/auth/me": () => jsonResponse(CURRENT_USER),
      "/health/ready": () => jsonResponse(READY),
    });
    const user = userEvent.setup();
    const { router } = renderApp("/status");

    await user.type(await screen.findByLabelText("Email"), "analyst@docintel.local");
    await user.type(screen.getByLabelText("Password"), "correct horse battery staple");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "System status" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/status");
    expect(screen.getByText("Finance Analyst")).toBeInTheDocument();

    const [loginCall] = calls(fetchMock, "/api/v1/auth/login");
    expect(JSON.parse(loginCall?.[1]?.body as string)).toEqual({
      email: "analyst@docintel.local",
      password: "correct horse battery staple",
    });
    // The web app asks for a session cookie; the token itself is never stored.
    expect(loginCall?.[1]?.headers).toMatchObject({
      "X-Docintel-Session": "1",
    });
    const [meCall] = calls(fetchMock, "/api/v1/auth/me");
    expect(meCall?.[1]?.headers).toMatchObject({
      Authorization: "Bearer header.payload.signature",
    });
    expect(window.sessionStorage.length).toBe(0);
    expect(window.localStorage.length).toBe(0);
  });

  it("shows the server's message for invalid credentials", async () => {
    mockFetch({
      "/api/v1/auth/login": () => problem(401, "Invalid email or password."),
    });
    const user = userEvent.setup();
    renderApp("/login");

    await user.type(await screen.findByLabelText("Email"), "analyst@docintel.local");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password.");
  });

  it("restores the session from the cookie after a reload", async () => {
    signedIn();
    const fetchMock = mockFetch({
      "/api/v1/auth/me": () => jsonResponse(CURRENT_USER),
      "/health/ready": () => jsonResponse(READY),
    });
    renderApp("/status");

    expect(await screen.findByRole("heading", { name: "System status" })).toBeInTheDocument();
    const [refreshCall] = calls(fetchMock, "/api/v1/auth/refresh");
    expect(refreshCall?.[1]).toMatchObject({
      method: "POST",
      credentials: "same-origin",
    });
    expect(refreshCall?.[1]?.headers).toMatchObject({
      "X-Docintel-Session": "1",
    });
  });

  it("renews a rejected token from the cookie once, then gives up", async () => {
    let issued = 0;
    let attempts = 0;
    const fetchMock = mockFetch({
      // Each refresh issues a new token; /me rejects every one of them.
      "/api/v1/auth/refresh": () => {
        issued += 1;
        return jsonResponse({
          ...TOKEN_RESPONSE,
          access_token: `token-${String(issued)}`,
        });
      },
      "/api/v1/auth/me": () => {
        attempts += 1;
        return problem(401, "Invalid or expired token.");
      },
    });
    const { router } = renderApp("/status");

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/login");
    });
    // Restore + one renewal after the rejection; the renewed token is rejected too: signed out,
    // with no further renewals.
    expect(calls(fetchMock, "/api/v1/auth/refresh")).toHaveLength(2);
    expect(attempts).toBe(2);
  });

  it("signs out from the header and revokes the session cookie", async () => {
    signedIn();
    const fetchMock = mockFetch({
      "/api/v1/auth/me": () => jsonResponse(CURRENT_USER),
      "/health/ready": () => jsonResponse(READY),
    });
    const user = userEvent.setup();
    const { router } = renderApp("/status");

    await user.click(await screen.findByRole("button", { name: "Sign out" }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/login");
    });
    const [logoutCall] = calls(fetchMock, "/api/v1/auth/logout");
    expect(logoutCall?.[1]).toMatchObject({ method: "POST" });
    expect(logoutCall?.[1]?.headers).toMatchObject({
      "X-Docintel-Session": "1",
    });
  });

  it("renews the token a minute before it expires", () => {
    const now = 1_000_000;
    expect(refreshDelay({ token: "t", userId: "u", expiresAt: now + 15 * 60_000 }, now)).toBe(14 * 60_000);
    // Short lifetimes renew at 80%, never sooner than a second.
    expect(refreshDelay({ token: "t", userId: "u", expiresAt: now + 60_000 }, now)).toBe(48_000);
    expect(refreshDelay({ token: "t", userId: "u", expiresAt: now }, now)).toBe(1_000);
  });
});

describe("system status page", () => {
  it("renders readiness checks and the user's permissions", async () => {
    signedIn();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(CURRENT_USER),
      "/health/ready": () => jsonResponse(READY),
    });
    renderApp("/status");

    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("database")).toBeInTheDocument();
    expect(screen.getByText("4.2 ms")).toBeInTheDocument();
    expect(screen.getByText("API version 0.1.0")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Permissions" })).toHaveTextContent("documents:upload");
  });

  it("shows failing checks when the backend is not ready", async () => {
    signedIn();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(CURRENT_USER),
      "/health/ready": () =>
        jsonResponse(
          {
            status: "not_ready",
            version: "0.1.0",
            checks: {
              database: {
                status: "fail",
                detail: "database unavailable",
                latency_ms: null,
              },
              migrations: {
                status: "fail",
                detail: "unknown (database unavailable)",
                latency_ms: null,
              },
            },
          },
          503,
        ),
    });
    renderApp("/status");

    expect(await screen.findByText("Not ready")).toBeInTheDocument();
    expect(screen.getByText("database unavailable")).toBeInTheDocument();
    expect(screen.getAllByText("Failing")).toHaveLength(2);
  });
});
