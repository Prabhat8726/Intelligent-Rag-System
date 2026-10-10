import { readFileSync } from "node:fs";

import { expect, test, type Browser, type Page } from "@playwright/test";

/**
 * The master prompt's final demonstration (§50), through the web app:
 * PO + invoice with a controlled price difference → upload → classify → extract → normalize →
 * compare → mismatch detected → procurement policy retrieved → the agent analyses and
 * recommends → a person reviews and approves → workflow result → audit log → dashboard.
 */

const PASSWORD = process.env.SEED_USER_PASSWORD ?? "";
const RUN = process.env.E2E_RUN ?? "local";

function file(variable: string, suffix: string) {
  const location = process.env[variable];
  if (!location) throw new Error(`${variable} is not set (global setup)`);
  // A run-specific name: the inbox may already hold earlier demo documents.
  return { name: `demo-${RUN}-${suffix}.pdf`, mimeType: "application/pdf", buffer: readFileSync(location) };
}

async function signIn(browser: Browser, email: string): Promise<Page> {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  return page;
}

async function upload(page: Page, document: ReturnType<typeof file>): Promise<void> {
  await page.goto("/documents");
  const form = page.getByRole("form", { name: "Upload document" });
  await form.getByLabel(/^Document/).setInputFiles(document);
  await form.getByRole("button", { name: "Upload" }).click();
  // The inbox polls while documents are processed; wait for this one to finish.
  const row = page.getByRole("row").filter({ hasText: document.name });
  await expect(row).toContainText(/Processed|Needs review/, { timeout: 120_000 });
}

test("the demo path: a price difference from upload to an approved workflow", async ({ browser }) => {
  const purchaseOrder = file("E2E_DEMO_PO", "PO");
  const deliveryNote = file("E2E_DEMO_DN", "DN");
  const invoice = file("E2E_DEMO_INV", "INV");

  // 1-7. An analyst uploads the documents; they are classified, read and normalized.
  const analyst = await signIn(browser, "analyst@docintel.local");
  await upload(analyst, purchaseOrder);
  await upload(analyst, deliveryNote);
  await upload(analyst, invoice);
  const invoiceRow = analyst.getByRole("row").filter({ hasText: invoice.name });
  await expect(invoiceRow).toContainText("Invoice");
  await expect(invoiceRow).toContainText("Needs review");
  await invoiceRow.getByRole("link", { name: invoice.name }).click();
  await expect(analyst.getByRole("heading", { name: invoice.name })).toBeVisible();

  // 8-9. Compared with its order and delivery: the price difference is found.
  const checks = analyst.getByRole("region", { name: "Checks and review" });
  await expect(checks).toContainText("Unit price differs from the purchase order");
  await checks.getByRole("link", { name: /match/i }).first().click();
  await expect(analyst.getByLabel("Summary")).toContainText(/mismatch/i);
  await analyst.goBack();

  // 10-12. The invoice processing workflow investigates (rules, policy) and recommends.
  await analyst.getByRole("button", { name: "Start invoice processing" }).click();
  await expect(analyst).toHaveURL(/\/workflows\/[0-9a-f-]+$/);
  const workflowUrl = analyst.url();
  const proposal = analyst.getByRole("region", { name: "Proposed action" });
  await expect(proposal).toContainText("Ask the vendor to clarify", { timeout: 120_000 });
  await expect(proposal).toContainText(/awaiting approval/i);
  await expect(analyst.getByRole("region", { name: "Sources" })).toContainText(
    /Price variance|Price or quantity difference|Discrepancies/,
  );
  // The analyst started the workflow and uploaded the invoice: they cannot decide it.
  await expect(analyst.getByLabel("Why you cannot decide")).toContainText("maker-checker");
  await expect(analyst.getByRole("form", { name: "Decision" })).toHaveCount(0);

  // 13-14. A reviewer reviews the evidence and approves.
  const reviewer = await signIn(browser, "reviewer@docintel.local");
  await reviewer.goto("/workflows");
  await reviewer.getByRole("tab", { name: "Awaiting my decision" }).click();
  const queue = reviewer.getByRole("region", { name: "Workflow list" });
  await queue.getByRole("link", { name: new RegExp(invoice.name) }).first().click();
  await expect(reviewer).toHaveURL(workflowUrl);
  const decision = reviewer.getByRole("form", { name: "Decision" });
  await decision.getByLabel("Reason (required to reject)").fill("Price checked against the order: ask the vendor.");
  await decision.getByRole("button", { name: "Approve" }).click();

  // 15. The workflow result: a drafted message for the vendor, the invoice stays in review.
  const result = reviewer.getByLabel("Result");
  await expect(result).toContainText("Message for the vendor (not sent)");
  await expect(reviewer.getByText("Waiting for the vendor").first()).toBeVisible();
  await reviewer.getByRole("region", { name: "Reports" }).getByRole("link").first().click();
  await reviewer.getByRole("button", { name: "Verify" }).click();
  await expect(reviewer.getByRole("status").filter({ hasText: "Verified" })).toBeVisible();

  // 16. The audit log records the decision.
  const admin = await signIn(browser, "admin@docintel.local");
  await admin.goto("/admin/audit");
  await admin.getByLabel("Event type").selectOption("workflow.");
  const events = admin.getByRole("region", { name: "Audit events" });
  await expect(events.getByRole("row").filter({ hasText: "workflow.action.approved" }).first()).toContainText(
    "reviewer@docintel.local",
  );

  // 17. The dashboard shows it; a reload keeps the session (httpOnly refresh cookie).
  await reviewer.goto("/dashboard");
  await reviewer.reload();
  const activity = reviewer.getByRole("list", { name: "Recent activity" });
  await expect(activity.getByRole("listitem").filter({ hasText: invoice.name }).first()).toContainText(
    /approved a proposal on|completed a workflow on/,
  );
  await expect(reviewer.getByRole("list", { name: "Failing rules" })).toContainText("INV_PO_UNIT_PRICE");
});
