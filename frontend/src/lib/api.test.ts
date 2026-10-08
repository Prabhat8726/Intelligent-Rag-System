import { describe, expect, it } from "vitest";

import { jsonResponse, mockFetch, problem } from "../test/utils";
import { ApiError, apiRequest } from "./api";

describe("apiRequest", () => {
  it("sends JSON bodies and the bearer token", async () => {
    const fetchMock = mockFetch({ "/api/v1/things": () => jsonResponse({ ok: true }) });

    const result = await apiRequest<{ ok: boolean }>("/api/v1/things", {
      method: "POST",
      body: { name: "x" },
      token: "abc.def.ghi",
    });

    expect(result).toEqual({ ok: true });
    const init = fetchMock.mock.calls[0]?.[1];
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ name: "x" }));
    expect(init?.headers).toMatchObject({
      Authorization: "Bearer abc.def.ghi",
      "Content-Type": "application/json",
    });
  });

  it("raises ApiError carrying the problem detail", async () => {
    mockFetch({ "/api/v1/secure": () => problem(403, "You do not have permission.") });

    const error = await apiRequest("/api/v1/secure").catch((err: unknown) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(403);
    expect((error as ApiError).message).toBe("You do not have permission.");
    expect((error as ApiError).problem?.request_id).toBe("req-1");
  });

  it("handles non-JSON error bodies", async () => {
    mockFetch({ "/api/v1/gateway": () => new Response("<html>Bad gateway</html>", { status: 502 }) });

    const error = await apiRequest("/api/v1/gateway").catch((err: unknown) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).problem).toBeNull();
    expect((error as ApiError).message).toBe("Request failed with status 502");
  });

  it("returns bodies for explicitly accepted error statuses", async () => {
    mockFetch({ "/health/ready": () => jsonResponse({ status: "not_ready" }, 503) });

    await expect(apiRequest("/health/ready", { acceptStatuses: [503] })).resolves.toEqual({
      status: "not_ready",
    });
  });
});
