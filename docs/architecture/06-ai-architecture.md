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
| `VisionProvider` | Gemini multimodal (3) | Ollama vision model (4, optional) |
| `OCRProvider` | Tesseract 5 via `pytesseract` (3) | — (already local) |

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
* **Sensitivity routing** (Phase 3): a router picks external vs local provider
  per document sensitivity (`AI_EXTERNAL_MAX_SENSITIVITY`), see C1.

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

## 3. OCR & multimodal understanding (Module 3, Phase 3)

Per **page**, not per document (mixed PDFs are common):

1. **Inspect**: `pdfplumber` char count + text quality heuristics (printable
   ratio, dictionary-word ratio) decide `NATIVE` vs `OCR`.
2. **Native**: `pdfplumber` words with bounding boxes (PDF points, top-left origin).
3. **OCR**: render with `pypdfium2` at 300 DPI → Pillow preprocessing
   (grayscale, deskew via Tesseract OSD, adaptive threshold) → Tesseract
   `image_to_data` → words, boxes, per-word confidence.
4. **Vision fallback**: pages with mean OCR confidence below a threshold, or
   detected tables in scanned pages, go to `VisionProvider` with a table/field
   schema — only if the sensitivity gate allows.
5. **Normalized representation**: `PageContent{page_number, width, height,
   method, words[], lines[], blocks[], tables[], ocr_confidence}` — the single
   format consumed by every downstream stage regardless of source.

Licences: `pypdfium2` (Apache-2.0/BSD-3), `pdfplumber` (MIT), Tesseract
(Apache-2.0). PyMuPDF rejected (AGPL-3.0).

## 4. Classification (Module 4, Phase 3)

* **Stage 1 — local model**: TF-IDF (word + char n-grams) + logistic regression,
  probability-calibrated (`CalibratedClassifierCV`). Trained on the synthetic
  corpus + stored human corrections. Microseconds, free, private.
* **Stage 2 — LLM fallback** when local max-probability < `CLASSIFICATION_MIN_CONFIDENCE`
  (and sensitivity allows): fast model with an enum-constrained schema.
* **Final confidence** = local calibrated probability when stage 1 decides;
  when the LLM decides, confidence comes from *agreement* signals (LLM label vs
  local top-2, keyword evidence), not from the LLM's own number.
* Human correction → `document_classifications(method=HUMAN)` → retraining set.

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
