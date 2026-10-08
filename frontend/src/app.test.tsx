import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { saveSession } from "./auth/session";
import { CURRENT_USER, jsonResponse, mockFetch, problem, renderApp } from "./test/utils";

const READY = {
  status: "ready",
  version: "0.1.0",
  checks: {
    database: { status: "ok", detail: null, latency_ms: 4.2 },
    migrations: { status: "ok", detail: null, latency_ms: null },
  },
};

const TOKEN_RESPONSE = {
  access_token: "header.payload.signature",
  token_type: "bearer",
  expires_in: 1800,
  user: CURRENT_USER,
};

function signedIn() {
  saveSession({ token: "header.payload.signature", expiresAt: Date.now() + 60_000 });
}

describe("authentication flow", () => {
  it("redirects anonymous users to the login page", async () => {
    mockFetch({});
    const { router } = renderApp("/status");

    expect(await screen.findByRole("heading", { name: "Document Intelligence" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
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

    const loginCall = fetchMock.mock.calls.find(([url]) => url === "/api/v1/auth/login");
    expect(JSON.parse(loginCall?.[1]?.body as string)).toEqual({
      email: "analyst@docintel.local",
      password: "correct horse battery staple",
    });
    const meCall = fetchMock.mock.calls.find(([url]) => url === "/api/v1/auth/me");
    expect(meCall?.[1]?.headers).toMatchObject({ Authorization: "Bearer header.payload.signature" });
  });

  it("shows the server's message for invalid credentials", async () => {
    mockFetch({ "/api/v1/auth/login": () => problem(401, "Invalid email or password.") });
    const user = userEvent.setup();
    renderApp("/login");

    await user.type(await screen.findByLabelText("Email"), "analyst@docintel.local");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password.");
  });

  it("ends the session when the API rejects the stored token", async () => {
    signedIn();
    mockFetch({ "/api/v1/auth/me": () => problem(401, "Invalid or expired token.") });
    const { router } = renderApp("/status");

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/login");
    });
    expect(window.sessionStorage.length).toBe(0);
  });

  it("discards expired stored sessions without calling the API", async () => {
    saveSession({ token: "old", expiresAt: Date.now() - 1 });
    const fetchMock = mockFetch({});
    const { router } = renderApp("/status");

    await screen.findByLabelText("Email");
    expect(router.state.location.pathname).toBe("/login");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("signs out from the header", async () => {
    signedIn();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(CURRENT_USER),
      "/health/ready": () => jsonResponse(READY),
    });
    const user = userEvent.setup();
    const { router } = renderApp("/status");

    await user.click(await screen.findByRole("button", { name: "Sign out" }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/login");
    });
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
              database: { status: "fail", detail: "database unavailable", latency_ms: null },
              migrations: { status: "fail", detail: "unknown (database unavailable)", latency_ms: null },
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
