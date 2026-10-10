import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

/**
 * Prepares the demo (master prompt §50): a fresh purchase order, delivery note and invoice with
 * a planted price difference (a random seed, so re-runs never collide with earlier uploads),
 * and the policy knowledge base on the stack. Uses the backend CLI on this machine
 * (DOCINTEL_CLI, default `uv run --project ../backend docintel`).
 */

interface ManifestEntry {
  file: string;
  document_type: string;
}

function cli(args: string[]): void {
  const command = (process.env.DOCINTEL_CLI ?? "uv run --project ../backend docintel").split(" ");
  const [program, ...prefix] = command;
  if (!program) throw new Error("DOCINTEL_CLI is empty");
  const result = spawnSync(program, [...prefix, ...args], { stdio: "inherit", env: process.env });
  if (result.status !== 0) {
    throw new Error(`docintel ${args[0] ?? ""} failed (exit ${String(result.status)})`);
  }
}

export default function globalSetup(): void {
  if (!process.env.SEED_USER_PASSWORD) {
    throw new Error("Set SEED_USER_PASSWORD (the demo users' password, from .env).");
  }
  const baseUrl = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8080";
  const folder = mkdtempSync(path.join(tmpdir(), "docintel-e2e-"));
  const seed = String(Math.floor(Math.random() * 1_000_000_000));
  cli(["generate-documents", "--output", folder, "--seed", seed, "--scenario", "UNIT_PRICE_MISMATCH"]);
  // Idempotent: files already on the stack are reported and skipped.
  cli(["knowledge-ingest", path.resolve("../knowledge_base"), "--api-url", baseUrl]);

  const manifest = JSON.parse(readFileSync(path.join(folder, "manifest.json"), "utf8")) as {
    documents: ManifestEntry[];
  };
  const byType = (type: string) => {
    const entry = manifest.documents.find((document) => document.document_type === type);
    if (!entry) throw new Error(`the generator wrote no ${type}`);
    return path.join(folder, entry.file);
  };
  process.env.E2E_DEMO_PO = byType("PURCHASE_ORDER");
  process.env.E2E_DEMO_DN = byType("DELIVERY_NOTE");
  process.env.E2E_DEMO_INV = byType("INVOICE");
  process.env.E2E_RUN = seed.slice(-6);
}
