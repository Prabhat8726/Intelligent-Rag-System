# End-to-end tests (Playwright)

`demo.spec.ts` drives the master prompt's demonstration through the web app: a purchase order,
delivery note and invoice with a planted price difference are uploaded, classified, extracted
and compared; the invoice processing workflow investigates it with the policy knowledge base
and proposes asking the vendor; a reviewer approves; the result, the audit log and the
dashboard are checked, and a reload keeps the session.

Run it against a running stack with the demo users:

```bash
make up && make seed-docker          # or: make dev (then E2E_BASE_URL=http://localhost:5173)
cd frontend && npx playwright install chromium   # once
make e2e                             # from the repository root; reads SEED_USER_PASSWORD from .env
```

The global setup generates a fresh bundle with a random seed (`docintel generate-documents
--scenario UNIT_PRICE_MISMATCH`, so repeated runs never collide as duplicates) and loads the
knowledge base if it is missing (`docintel knowledge-ingest`, idempotent). It calls the backend
CLI on this machine: `DOCINTEL_CLI` (default `uv run --project ../backend docintel`).

Failures keep a trace, screenshots and an HTML report in `e2e-results/` and `e2e-report/`
(`npx playwright show-report e2e-report`). Use synthetic documents only.
