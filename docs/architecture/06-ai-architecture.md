# 06 — AI / ML / LLM Architecture

Principle: **deterministic first, model second, LLM last** — and every model
output is validated, grounded, and auditable. The LLM never decides a fact that
code can compute.

## 1. Provider abstraction (Module 43)

```mermaid
classDiagram
  class LLMProvider {
    <<protocol>>
    +name: str
    +generate(request: LLMRequest) LLMResponse
    +generate_structured(request, schema: type[BaseModel]) StructuredResult
  }
  class EmbeddingProvider {
    <<protocol>>
    +name: str
    +model: str
    +dimensions: int
    +embed(texts, task: EmbeddingTask) list[vector]
  }
  class VisionProvider {
    <<protocol>>
    +generate_structured(images, prompt, schema) StructuredResult
  }
  class OCRProvider {
    <<protocol>>
    +recognize(image) OCRPage  // words, boxes, confidences
  }
  LLMProvider <|.. GeminiLLMProvider
  EmbeddingProvider <|.. GeminiEmbeddingProvider
  VisionProvider <|.. GeminiVisionProvider
  OCRProvider <|.. TesseractOCRProvider
  EmbeddingProvider <|.. FastEmbedProvider
  LLMProvider <|.. OllamaLLMProvider
```

| Interface | Default (Phase) | Local alternative (Phase) |
|---|---|---|
| `LLMProvider` | Gemini via `google-genai` 2.x (**0**) | Ollama HTTP API (4) |
| `EmbeddingProvider` | Gemini `gemini-embedding-001`, 768-d, L2-normalized (**0**) | `fastembed` `BAAI/bge-base-en-v1.5`, 768-d (6) |
| `VisionProvider` | Gemini multimodal (4, with extraction — ADR-024) | Ollama vision model (4, optional) |
| `OCRProvider` | Tesseract 5 via subprocess (**3**) | — (already local) |

Cross-cutting concerns implemented **once** in the provider layer:

* **Structured output**: JSON schema derived from Pydantic models
  (`response_mime_type=application/json`, `response_json_schema`), then
  *re-validated* locally with Pydantic. Malformed JSON → typed
  `StructuredOutputError` carrying the raw text for one repair attempt by the
  caller; never a crash, never silently accepted.
* **Retries**: transient HTTP codes (408/429/500/502/503/504) retried by the SDK
  with exponential backoff + jitter; attempts/timeouts configurable.
* **Client-side rate limiting**: async token bucket (`LLM_REQUESTS_PER_MINUTE`)
  so free-tier RPM limits are respected proactively instead of via 429 storms.
* **Usage accounting**: every call returns `LLMUsage` (input/output/thinking
  tokens, latency, model); Phase 4 persists it to `llm_calls` and Prometheus.
  Prompts/completions are **not** stored.
* **Error taxonomy**: `ProviderConfigurationError`, `ProviderRateLimitError`,
  `ProviderUnavailableError`, `ProviderResponseError`, `StructuredOutputError`
  — callers branch on type, not on SDK internals.
* **Sensitivity routing** (Phase 3, implemented): `ExternalAIGate` allows external calls only up
  to `AI_EXTERNAL_MAX_SENSITIVITY`, see C1 and §4.

## 2. Model selection (verified 2026-10-08, configurable)

| Purpose | Setting | Default | Why |
|---|---|---|---|
| Extraction, agent analysis, RAG answers | `GEMINI_MODEL` | `gemini-3.5-flash` | Stable 3.x Flash with the longest published lifetime (shutdown no earlier than May 2027) |
| Classification fallback, query planning | `GEMINI_FAST_MODEL` | `gemini-3.5-flash-lite` | Cheapest/fastest stable 3.5-family model, aimed at classification/extraction |
| Embeddings | `GEMINI_EMBEDDING_MODEL` | `gemini-embedding-001` | GA, documented in the SDK; supports `output_dimensionality=768` (needs client-side normalization, which we always do) |

Why pin versions instead of `gemini-flash-latest`: evaluations must be
reproducible; an alias silently changes behaviour. `make check-ai` lists the
models available to *your* key — if Google retires a default, change one env var.
Free-tier quotas differ per model and change often; check AI Studio for current
limits instead of trusting numbers in docs.

Gemini 3.x notes honoured by the implementation: sampling parameters
(`temperature/top_p/top_k`) are only sent if explicitly configured; thinking is
controlled by `thinking_level` (`LOW/MEDIUM/HIGH`), optional.

## 3. OCR & document understanding (Module 3, Phase 3 — implemented)

Per **page**, not per document (mixed PDFs are common). Code: `docintel/processing/`.

1. **Inspect** (`inspection.py`): per-page character and image counts decide `NATIVE` vs
   `OCR` (ADR-017).
2. **Native text** (`native.py`): pypdfium2 character boxes (loose boxes: advance width and
   font ascent/descent) grouped into words, converted to the page's displayed orientation with
   a top-left origin. A text layer whose characters are mostly unusable (private-use glyphs,
   replacement characters) is OCR'd instead. pdfplumber is not used (ADR-021).
3. **OCR** (`ocr.py`, `extraction.py`, `preprocess.py`): PDF pages rendered at 300 DPI; images
   used as stored (EXIF orientation applied) and upscaled up to 2x below 250 DPI; projection-
   profile **deskew** (rotation within ±5°); Tesseract 5 via subprocess (`tsv` output, per-page
   timeout, `OMP_THREAD_LIMIT=1`, pages in parallel); a poor reading triggers orientation
   detection (`--psm 0`) and the better of the two readings is kept; ruling-line debris and
   noise tokens are dropped. Every step was kept or rejected by measurement
   (`evaluation/reports/ocr.md`): median filtering, autocontrast and ruling-line removal made
   synthetic scans worse and are off.
4. **Layout** (`layout.py`): skew-tolerant line grouping, column segments (gap > 0.75 × text
   size), blocks in reading order, label/value grids read row by row, headings by size.
5. **Tables** (`tables.py`): one geometry-based detector for native and OCR pages (header row
   of short labels, column boundaries that cut the fewest words, wrapped cells, row-rhythm
   stop, a column for row numbers whose header OCR lost), stitched across pages under the same
   header (ADR-021).
6. **Normalized representation** (`content.py`): `PageContent{page_number, width, height, unit,
   method, words[], lines[], blocks[], tables[], ocr_confidence, rotation_applied,
   deskew_degrees}` — the single format consumed by every downstream stage. Persisted in
   `document_pages` (words and layout as JSONB) and `document_tables`/`table_rows`; a PNG
   preview per page goes to document storage.

**Vision fallback deferred to Phase 4.** The plan sent low-confidence OCR pages to a vision
model here. In Phase 4 the extraction prompt attaches page images for low-confidence pages
anyway, which covers the same need with one call instead of two (ADR-024). Phase 3 flags such
pages (`LOW_OCR_CONFIDENCE` review reason).

Licences: pypdfium2 (Apache-2.0/BSD-3), Tesseract (Apache-2.0), scikit-learn (BSD-3),
RapidFuzz (MIT). PyMuPDF rejected (AGPL-3.0).

## 4. Classification (Module 4, Phase 3 — implemented)

Code: `docintel/classification/`.

* **Stage 1 — local model**: TF-IDF (word 1–2-grams + character 3–5-grams on lower-cased text
  with digits collapsed) + logistic regression, Platt-calibrated (`CalibratedClassifierCV`,
  sigmoid, 3-fold). Trained **when the worker starts** on a seeded synthetic corpus covering
  all nine types plus current human corrections (ADR-022); ~9–20 s, deterministic, no model
  file to trust. Every prediction records the model fingerprint.
* **Stage 2 — LLM fallback** when the local probability is below
  `CLASSIFICATION_MIN_CONFIDENCE` (0.7) **and** the sensitivity gate allows external AI: the
  fast model picks one label from the fixed list (enum-constrained structured output) and
  quotes the text that supports it. The document text is passed as untrusted data.
* **Confidence**: the local calibrated probability when stage 1 decides. When the LLM decides,
  confidence comes from agreement signals only — agreement with the local top-1 (≥ 0.80) or
  runner-up (0.65), keyword evidence (±0.10), and whether the quoted evidence is actually in
  the text (−0.10 if not) — never from the LLM's own certainty (ADR-005).
* **Routing**: below the threshold after both stages, or no LLM allowed/configured, the
  document becomes `REVIEW_REQUIRED` with reason `CLASSIFICATION_UNCERTAIN`; the best local
  guess is kept for the reviewer. LLM errors degrade to review; they never fail the job.
* **Human correction** (`PATCH /documents/{id}/classification`) creates a `HUMAN`
  classification that later processing never overrides, and becomes training data at the next
  worker start.
* **Sensitivity gate** (`docintel/ai/routing.py`): effective sensitivity = max(label set at
  upload, content findings, type minimum of the likely types); content findings are Luhn-valid
  payment card numbers and US SSNs (→ RESTRICTED); resumes and bank statements are at least
  CONFIDENTIAL; IBANs and e-mail addresses are recorded without raising the level. Only counts
  and page numbers are stored. Above `AI_EXTERNAL_MAX_SENSITIVITY` nothing leaves the worker.

## 5. Structured extraction (Modules 6–8, Phase 4)

* **Schemas** (Pydantic, versioned): `InvoiceV1`, `PurchaseOrderV1`, `ReceiptV1`,
  `DeliveryNoteV1`, `ContractV1`, `ResumeV1`, `BankStatementV1`, `PolicyV1`.
  Each field is wrapped as `ExtractedValue{value, page, source_text}` so the
  model must cite where it read the value.
* **Prompt**: page-tagged text (`<page n="1">…</page>`) inside a clearly
  delimited *untrusted data* block; tables passed as Markdown; images added only
  for low-OCR-confidence pages.
* **Validation**: Pydantic → one repair round-trip with the validation errors →
  else `PARTIAL`/`FAILED` + review task.
* **Evidence verification** (anti-hallucination): `source_text` must be found in
  the cited page (exact → `VERIFIED`; `rapidfuzz` partial ratio ≥ threshold →
  `FUZZY`; else `NOT_FOUND`), and the value must be derivable from the
  source_text (e.g. normalized amount equals parsed number in the quote). Boxes
  come from matching words.
* **Normalization**: dates (explicit format detection; ambiguous `03/04/2026`
  → `UNCERTAIN` unless vendor/locale hints resolve it), currency (ISO 4217 from
  symbol/code/vendor default), amounts (`Decimal`, US/EU separators), vendor
  names (casefold, strip legal suffixes, `rapidfuzz` match against `vendors`).
  `original_value` is always kept.
* **Arithmetic validation**: Σ line totals = subtotal; subtotal + tax = total;
  qty × unit price = line total (tolerances configurable).

## 6. Confidence model (Module 26)

Field confidence is a weighted combination of **measured** signals:

| Signal | Source | Range |
|---|---|---|
| `ocr` | mean word confidence of the evidence span (1.0 for native text) | 0–1 |
| `evidence` | VERIFIED 1.0 / FUZZY ratio / NOT_FOUND 0 | 0–1 |
| `type_valid` | value parses as the declared type/format | 0/1 |
| `consistency` | arithmetic / cross-field checks involving the field | 0/1/neutral |
| `agreement` | agreement with an independent extractor (regex/heuristic) where one exists | 0/1/neutral |

* Document confidence = minimum over **required** fields (weakest link) —
  conservative by design.
* LLM self-reported confidence is **not** an input.
* Routing: `≥ CONFIDENCE_HIGH` and no blocking rule → auto; `≥ CONFIDENCE_MEDIUM`
  → analyst review; below → mandatory review. Any `CRITICAL` rule failure forces review.
* Phase 10 calibrates weights/thresholds on held-out labelled data and reports
  reliability (ECE) and the error rate inside the auto-processed bucket.

## 7. Cost & quota control (free-tier strategy)

1. Deterministic stages first (native text, rules, comparison, normalization) — zero LLM calls.
2. Local classifier handles confident cases; LLM only for the uncertain tail.
3. One extraction call per document version (all fields at once), cached by
   `(sha256, schema_version, prompt_version, model)`.
4. Embeddings batched; re-embedding only when content hash or model changes.
5. Client-side token bucket + SDK retries; daily budget guard (`LLM_DAILY_REQUEST_BUDGET`, Phase 4).
6. Every call accounted (`llm_calls`) → dashboard shows calls, tokens, estimated cost
   (labelled "estimated at paid-tier list price" when on the free tier).
