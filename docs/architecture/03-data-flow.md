# 03 — End-to-End Data Flow

Five flows cover the system. Every step that changes state writes an audit record.

## Flow A — Upload & ingestion (API, synchronous part)

```mermaid
sequenceDiagram
  autonumber
  actor U as User (ANALYST)
  participant W as nginx
  participant A as API
  participant S as DocumentStorage
  participant DB as PostgreSQL

  U->>W: POST /api/v1/documents (multipart)
  W->>W: client_max_body_size check
  W->>A: forward
  A->>A: authenticate JWT, require documents:upload
  A->>A: stream to temp file: enforce size limit, SHA-256, sniff magic bytes
  A->>A: validate extension ∩ magic MIME ∩ allowlist; PDF: not encrypted, page limit; image: pixel limit
  A->>DB: SELECT version by sha256 (exact duplicate check, access-scoped)
  A->>S: put(key = documents/{doc_id}/v1/{uuid}.{ext})  ← never the user filename
  A->>DB: BEGIN; INSERT documents, document_versions, processing_jobs(QUEUED), audit_logs; COMMIT
  alt DB commit fails
    A->>S: delete(key)  (compensation)
  end
  A-->>U: 201 {id, status: PENDING, duplicate_of?}
```

Key properties: the job is enqueued **in the same transaction** as the document
row (no "document without job" state); the storage key is server-generated (no
path traversal); the user-supplied filename is sanitized and stored only as
display metadata.

## Flow B — Processing pipeline (worker)

Stages 0–8 are implemented (Phases 2–4); 9–11 are planned (Phases 5–6). Stage names in
brackets are the names recorded in `processing_jobs.stage_timings`.

```mermaid
flowchart TD
  Q[processing_jobs QUEUED] -->|claim: FOR UPDATE SKIP LOCKED + lease| P[doc → PROCESSING]
  P --> G[0 Integrity: re-verify SHA-256 of the stored file  ·integrity·]
  G --> I[1 Inspect per page: text layer? images? rotation?  ·inspect·]
  I --> T{usable text<br/>layer?}
  T -- yes --> N[2a pypdfium2 words + boxes]
  T -- no --> O[2b render · upscale · deskew → Tesseract OCR<br/>words + boxes + confidence]
  N & O --> L[3 Layout: lines, column segments, blocks, label/value grids;<br/>geometry tables stitched across pages  ·extract· + ·previews·]
  L --> C[4 Classify: local calibrated model → LLM fallback if uncertain  ·classify·]
  C --> LX[5 Layout extractor: labels, letterhead, table columns  ·fields·]
  LX --> M{confident?<br/>EXTRACTION_LLM_MODE}
  M -- auto: no / always --> SG{sensitivity gate:<br/>external AI allowed<br/>or local model?}
  SG -- yes --> X[5' LLM: schema-constrained output,<br/>page-tagged text + low-confidence page images,<br/>one repair round-trip, cached by input hash]
  SG -- no --> MG
  M -- yes / never --> MG
  X --> MG[6 Merge field by field; evidence: quote located on the page → page, box, status]
  MG --> NM[7 Normalize: amounts, currency, dates, terms; vendor master match]
  NM --> CF[8 Consistency checks → confidence per field + document → review level]
  CF --> D[9 Duplicate detection: invoice#+vendor · fuzzy amount/date  — Phase 5]
  D --> CH[10 Chunk + embed for semantic search  — Phase 6]
  CH --> WF[11 Auto-workflow hook: invoice → PO → comparison + rules  — Phase 5]
  WF --> R{routing}
  R -- AUTO, no open reason --> DONE[doc COMPLETED]
  R -- review level or other reason --> REV[doc REVIEW_REQUIRED + reasons]
  P -. unrecoverable error / attempts exhausted .-> F[doc FAILED, job FAILED, error recorded]
```

* Every stage is **idempotent**: its results are replaced per document version, so a
  retried or re-requested job simply runs again. Human input survives: a human
  classification is never overridden, and human field corrections are re-applied to a new
  extraction of the same version and schema (ADR-032).
* Stage timings go to `processing_jobs.stage_timings` (shown on the document page) →
  dashboard "average processing time" and Prometheus histograms (Phases 9 and 11).
* Transient errors (HTTP 429/5xx, timeouts) → retry with exponential backoff and
  jitter, `run_after` pushed forward; permanent errors (validation, corrupt file)
  → fail fast.
* Lease expiry lets another worker reclaim a job whose worker crashed.

## Flow C — Knowledge ingestion & RAG query

```mermaid
sequenceDiagram
  autonumber
  actor M as MANAGER / ADMIN
  participant A as API
  participant WK as Worker
  participant E as EmbeddingProvider
  participant DB as PostgreSQL (pgvector + FTS)
  participant L as LLMProvider

  M->>A: POST /api/v1/knowledge/documents (policy PDF + metadata)
  A->>DB: knowledge_documents(PROCESSING) + job
  WK->>WK: extract text with structure (headings, sections, pages)
  WK->>WK: structure-aware chunking (section path, ~500 tokens, overlap)
  WK->>E: embed(chunks, task=RETRIEVAL_DOCUMENT) in batches
  WK->>DB: knowledge_chunks(content, tsvector, vector(768), metadata)
  Note over M,DB: --- query time ---
  M->>A: POST /api/v1/knowledge/query {question, filters}
  A->>E: embed(question, task=RETRIEVAL_QUERY)
  A->>DB: vector top-k  ∪  full-text top-k  (both pre-filtered by access, status, effective dates)
  A->>A: Reciprocal Rank Fusion → score threshold → context assembly [S1..Sn]
  alt evidence below threshold
    A-->>M: "insufficient evidence" + nearest sources (no generation)
  else
    A->>L: answer with schema {answer, claims[{text, citations}]}
    A->>A: validate citations ⊆ provided sources; drop/flag uncited claims
    A-->>M: answer + citations (doc, section, page, effective date)
  end
```

## Flow D — Agent investigation & human approval

```mermaid
sequenceDiagram
  autonumber
  actor U as ANALYST
  participant A as API
  participant WK as Worker (LangGraph)
  participant T as Tool registry
  participant DB as PostgreSQL
  actor R as MANAGER (approver)

  U->>A: POST /api/v1/analysis {query, document_ids}
  A->>DB: agent_runs(QUEUED) + job
  A-->>U: 202 {analysis_id}
  WK->>WK: understand_request (LLM → typed plan)
  loop bounded steps (max N)
    WK->>T: tool(args) as the requesting user
    T->>T: validate args · check permission · scope to user's documents
    T->>DB: read / record (tool call logged in agent_tool_calls)
  end
  WK->>WK: analyze (LLM, facts are read-only inputs) → findings w/ categories + citations
  WK->>WK: determine_confidence (deterministic) → recommendation (allowlisted action)
  alt action needs approval
    WK->>DB: workflow_actions(AWAITING_APPROVAL), review_task, run status AWAITING_APPROVAL
  else low-risk action
    WK->>DB: execute low-risk action (e.g. create review task / report)
  end
  WK->>DB: audit_logs
  U->>A: GET /api/v1/analysis/{id} → result
  R->>A: POST /api/v1/workflows/{id}/approve {reason}
  A->>A: maker-checker + role check + state transition check
  A->>DB: APPROVED → executor runs allowlisted action → EXECUTED / FAILED, transitions + audit
```

## Flow E — Final demonstration path (prompt §50)

| Step | Flow | Component |
|---|---|---|
| 1–2 Generate PO + invoice with controlled price mismatch | — | `synthetic` (`make generate-documents`) |
| 3 Upload both | A | API / storage |
| 4–7 Classify, OCR/extract, extract fields, normalize | B | worker / processing |
| 8–9 Compare, detect mismatch | B (auto-workflow) | comparison + rules |
| 10 Retrieve procurement policy | C | knowledge |
| 11–12 Agent analyzes, recommends | D | agent |
| 13–15 Human reviews, approves, workflow result | D | workflows |
| 16 Audit log records it | all | audit |
| 17 Dashboard displays it | — | SPA |

## Data classification along the flow

| Data | Where it lives | Leaves the platform? |
|---|---|---|
| Original file | object storage | never |
| Page text / words / boxes | `document_pages` | to external LLM **only if** sensitivity gate allows |
| Extracted fields | `extracted_fields` | same as above |
| Prompts / completions | not stored (only token counts, model, latency, status) | n/a |
| Logs | stdout (JSON) | no document content, secrets redacted |
