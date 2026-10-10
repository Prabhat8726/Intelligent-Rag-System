# Knowledge retrieval evaluation (RAG)

Generated 2026-10-10T14:15:43+00:00 from commit `2a74f53db57f` by `docintel evaluate --suite retrieval`. Do not edit by hand.

## Ranking (answerable questions; first 10 passages)

| configuration | dataset | n | hit@1 | hit@3 | hit@5 | section recall@5 | precision@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|---|---|---|---|
| hybrid (production: ts_rank_cd order) | kb-queries | 48 | 87.5% | 100.0% | 100.0% | 96.9% | 25.4% | 0.938 | 0.920 |
| hybrid (production: ts_rank_cd order) | kb-queries-holdout | 10 | 90.0% | 100.0% | 100.0% | 100.0% | 24.0% | 0.950 | 0.961 |
| hybrid, IDF order | kb-queries | 48 | 81.2% | 95.8% | 100.0% | 95.5% | 24.6% | 0.895 | 0.887 |
| hybrid, IDF order | kb-queries-holdout | 10 | 80.0% | 100.0% | 100.0% | 100.0% | 24.0% | 0.900 | 0.932 |
| full text only (production: IDF order) | kb-queries | 48 | 89.6% | 91.7% | 93.8% | 90.6% | 23.8% | 0.918 | 0.883 |
| full text only (production: IDF order) | kb-queries-holdout | 10 | 80.0% | 100.0% | 100.0% | 100.0% | 24.0% | 0.883 | 0.915 |
| full text only, ts_rank_cd order | kb-queries | 48 | 77.1% | 93.8% | 97.9% | 94.8% | 24.6% | 0.864 | 0.855 |
| full text only, ts_rank_cd order | kb-queries-holdout | 10 | 70.0% | 90.0% | 90.0% | 85.0% | 20.0% | 0.817 | 0.787 |
| dense only (hashing) | kb-queries | 48 | 79.2% | 95.8% | 100.0% | 98.3% | 25.8% | 0.885 | 0.886 |
| dense only (hashing) | kb-queries-holdout | 10 | 80.0% | 90.0% | 100.0% | 95.0% | 22.0% | 0.870 | 0.855 |
| hybrid, no title/breadcrumb prefix | kb-queries | 48 | 70.8% | 89.6% | 93.8% | 87.8% | 22.1% | 0.812 | 0.787 |
| hybrid, no title/breadcrumb prefix | kb-queries-holdout | 10 | 80.0% | 100.0% | 100.0% | 100.0% | 24.0% | 0.900 | 0.932 |
| hybrid, fixed-size chunks | kb-queries | 48 | 72.9% | 97.9% | 100.0% | 97.9% | 25.8% | 0.841 | 0.860 |
| hybrid, fixed-size chunks | kb-queries-holdout | 10 | 80.0% | 100.0% | 100.0% | 100.0% | 24.0% | 0.900 | 0.924 |

## Corpora (active versions; tokens estimated as characters / 4)

| corpus | chunks | mean tokens per chunk | max tokens |
|---|---|---|---|
| sections | 85 | 55.5 | 181.0 |
| no_prefix | 85 | 55.5 | 181.0 |
| fixed | 15 | 381.3 | 499.2 |

## Evidence gate (production configuration)

| dataset | unanswerable refused | refusal rate | answerable refused | false refusal rate | falsely refused | unanswerable accepted |
|---|---|---|---|---|---|---|
| kb-queries | 5/6 | 83.3% | 1/48 | 2.1% | q37 | q52 |
| kb-queries-holdout | 2/4 | 50.0% | 0/10 | 0.0% | - | h12, h14 |

## Access control and versions (production configuration)

| check | result |
|---|---|
| passages of another department's documents returned to a Finance user (68 questions) | 0 |
| SUPERSEDED passages returned as of 2026-10-01 | 0 |
| passages not yet in force returned as of 2025-06-30 | 0 |
| passages returned as of 2025-06-30 from the version then in force (now SUPERSEDED) | 408 |

## Latency (production configuration, local database, hashing embeddings)

| p50 ms | p95 ms | max ms | questions |
|---|---|---|---|
| 18.55 | 30.61 | 33.02 | 68 |

## Answerable questions with no relevant passage in the first 5 (production)

| id | question | expected sections |
|---|---|---|
| - | none | - |

## Notes

* Embeddings: offline lexical hashing model (hashing-ngram-v1). It matches words and word fragments, not meaning, so 'dense' here is a second lexical signal. Gemini and fastembed (BAAI/bge-base-en-v1.5) embeddings: Not yet measured.
* kb-queries was used to choose the evidence-gate thresholds (RAG_MIN_TERM_COVERAGE=0.25, RAG_MIN_DENSE_SIMILARITY=0.5) and the full-text ordering, so its numbers are optimistic. kb-queries-holdout was written afterwards and never used for tuning; it is small (10 answerable, 4 unanswerable questions).
* Relevance: a passage counts when it belongs to a labelled section (or a subsection). Fixed-size chunks have no sections; they count when they contain the section heading or the start of its first paragraph. Fixed-size chunks are larger on average and span several sections, which makes this criterion easier to meet; they also put more unrelated text into the model's context (see the corpora table).
* precision@5 is low by construction: most questions have one or two relevant sections, so at most one or two of five passages can be relevant.
* Retrieval date fixed at 2026-10-01 (versions in force); version check also run as of 2025-06-30. Generated answers (citation precision/recall, faithfulness) need an LLM: Not yet measured.

## Provenance

```json
{
  "dataset": {
    "knowledge_base": [
      "accounts-payable-faq 2026.1",
      "approval-matrix 2026.1",
      "compliance-policy 2026.1",
      "contract-management-guidelines 2026.1",
      "invoice-processing-procedure 2026.2",
      "legal-negotiation-playbook 2026.1",
      "procurement-glossary 2026.1",
      "procurement-policy 2025.1",
      "procurement-policy 2026.1",
      "records-retention 2026.1",
      "travel-and-expense-policy 2026.1"
    ],
    "documents": 11,
    "chunks_all_corpora": 198,
    "corpora": {
      "sections": {
        "chunks": 85,
        "mean_tokens": 55.5,
        "max_tokens": 181.0
      },
      "no_prefix": {
        "chunks": 85,
        "mean_tokens": 55.5,
        "max_tokens": 181.0
      },
      "fixed": {
        "chunks": 15,
        "mean_tokens": 381.3,
        "max_tokens": 499.2
      }
    },
    "queries": {
      "kb-queries": 54,
      "kb-queries-holdout": 14
    },
    "dataset_versions": {
      "kb-queries": "1.0",
      "kb-queries-holdout": "1.0"
    },
    "eval_date": "2026-10-01"
  },
  "config": {
    "embedding_model": "hashing-ngram-v1",
    "candidates": 20,
    "rrf_k": 60,
    "depth": 10,
    "chunking": {
      "target_tokens": 500,
      "max_tokens": 800,
      "overlap_tokens": 75,
      "fixed_size_baseline_chars": 2000,
      "fixed_size_overlap_chars": 300
    },
    "quick": false,
    "seconds": 13.6
  },
  "environment": {
    "python": "3.13.16",
    "pypdfium2": "5.14.0",
    "pillow": "12.3.0",
    "scikit-learn": "1.9.1",
    "numpy": "2.5.3",
    "rapidfuzz": "3.14.6",
    "reportlab": "5.0.1",
    "embedding": "hashing-ngram-v1"
  }
}
```
