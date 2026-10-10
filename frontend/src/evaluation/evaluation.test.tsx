import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EvaluationDetail, EvaluationSummary } from "../lib/types";
import { browserHasSession, CURRENT_USER, jsonResponse, mockFetch, renderApp } from "../test/utils";

const ME = { ...CURRENT_USER, permissions: ["documents:read", "evaluations:read"] };
const AGENT_ID = "1f6b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const OLDER_ID = "2a6b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4e";

function summary(overrides: Partial<EvaluationSummary> = {}): EvaluationSummary {
  return {
    id: AGENT_ID,
    suite: "agent",
    title: "Agent investigation evaluation",
    quick: false,
    git_revision: "fa098dfb7c7c",
    run_at: "2026-10-10T13:10:53Z",
    recorded_at: "2026-10-10T14:00:00Z",
    source: "IMPORT",
    recorded_by: null,
    gates: { passed: true, mode: "full", checks: 12, failed: 0 },
    headlines: [
      { label: "Agent: unsafe recommendations / planted defect reported", value: "0 / 100%" },
      { label: "Agent with a model", value: null },
    ],
    ...overrides,
  };
}

const DETAIL: EvaluationDetail = {
  ...summary({ gates: { passed: false, mode: "full", checks: 2, failed: 1 } }),
  dataset: { development_seed: 7 },
  config: { mode: "deterministic" },
  environment: { python: "3.13" },
  metrics: {},
  notes: ["Synthetic data only."],
  tables: [{ heading: "Overall (development dataset)", header: ["Measure", "Value"], rows: [["Runs", "70/70"]] }],
  gate_checks: [
    {
      metric: ["development", "unsafe_recommendations"],
      value: 0,
      min: null,
      max: 0,
      passed: true,
      problem: null,
      why: "No unsafe payment.",
    },
    {
      metric: ["development", "tool_selection", "recall"],
      value: 0.9,
      min: 0.99,
      max: null,
      passed: false,
      problem: "below 0.99",
      why: "The agent calls the tools a case needs.",
    },
  ],
  report_markdown: "# Agent investigation evaluation",
};

describe("evaluation", () => {
  it("shows the latest full run of every suite with its gates and headline numbers", async () => {
    browserHasSession();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(ME),
      "/api/v1/evaluations?latest=true&limit=100": () =>
        jsonResponse({
          items: [
            summary({ suite: "ocr", id: OLDER_ID, title: "OCR evaluation", gates: null, headlines: [] }),
            summary(),
          ],
          total: 2,
          limit: 100,
          offset: 0,
        }),
    });
    renderApp("/evaluation");

    const suites = await screen.findByRole("region", { name: "Evaluation suites" });
    const agent = within(suites).getByRole("article", { name: "Agent investigation evaluation" });
    expect(within(agent).getByText("12 gates passed")).toBeInTheDocument();
    expect(agent).toHaveTextContent("commit fa098df");
    expect(within(agent).getByText("0 / 100%")).toBeInTheDocument();
    expect(within(agent).getByText("Not in this run")).toBeInTheDocument();
    expect(within(agent).getByRole("link", { name: "Agent investigation evaluation" })).toHaveAttribute(
      "href",
      `/evaluation/${AGENT_ID}`,
    );
    expect(within(suites).getByText("No gates")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Evaluation" })).toHaveAttribute("href", "/evaluation");
  });

  it("explains how to load results when none are recorded", async () => {
    browserHasSession();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(ME),
      "/api/v1/evaluations?latest=true&limit=100": () => jsonResponse({ items: [], total: 0, limit: 100, offset: 0 }),
    });
    renderApp("/evaluation");
    expect(await screen.findByText(/No evaluation results are recorded yet/)).toBeInTheDocument();
  });

  it("shows one run: broken gates, the report's tables, notes, provenance and its history", async () => {
    browserHasSession();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse(ME),
      [`/api/v1/evaluations/${AGENT_ID}`]: () => jsonResponse(DETAIL),
      "/api/v1/evaluations?suite=agent&include_quick=true&limit=20": () =>
        jsonResponse({
          items: [summary(), summary({ id: OLDER_ID, quick: true, run_at: "2026-10-09T10:00:00Z", gates: null })],
          total: 2,
          limit: 20,
          offset: 0,
        }),
    });
    renderApp(`/evaluation/${AGENT_ID}`);

    expect(await screen.findByRole("heading", { name: "Agent investigation evaluation" })).toBeInTheDocument();
    expect(screen.getByText("1 of 2 gates broken")).toBeInTheDocument();
    const gates = screen.getByRole("region", { name: "Regression gates" });
    const broken = within(gates).getByText("development / tool_selection / recall").closest("tr");
    expect(broken).toHaveTextContent("≥ 0.99");
    expect(broken).toHaveTextContent("Broken: below 0.99");
    expect(within(gates).getByText("≤ 0")).toBeInTheDocument();

    const overall = screen.getByRole("region", { name: "Overall (development dataset)" });
    expect(within(overall).getByRole("cell", { name: "70/70" })).toBeInTheDocument();
    expect(screen.getByText("Synthetic data only.")).toBeInTheDocument();
    expect(screen.getByLabelText("Provenance")).toHaveTextContent('"development_seed": 7');
    expect(screen.getByText(/imported from the report files/)).toBeInTheDocument();

    const history = await screen.findByRole("region", { name: "Runs of this suite" });
    expect(within(history).getByText(/this run/)).toBeInTheDocument();
    expect(within(history).getByText("quick")).toBeInTheDocument();
    expect(within(history).getByRole("link")).toHaveAttribute("href", `/evaluation/${OLDER_ID}`);
  });

  it("is not in the navigation without the permission", async () => {
    browserHasSession();
    mockFetch({
      "/api/v1/auth/me": () => jsonResponse({ ...CURRENT_USER, permissions: ["documents:read", "dashboard:read"] }),
      "/api/v1/dashboard/summary?days=30": () => jsonResponse({}, 500),
    });
    renderApp("/dashboard");
    expect(await screen.findByRole("link", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Evaluation" })).not.toBeInTheDocument();
  });
});
