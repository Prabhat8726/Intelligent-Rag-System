# System performance (latency, throughput, failure rate)

Generated 2026-10-10T14:21:23+00:00 from commit `2a74f53db57f` by `docintel evaluate --suite system`. Do not edit by hand.

## Document pipeline

| Claim loops | Documents | Wall time | Documents / min | Pages / min | Native p50 / p95 | Scanned p50 / p95 | Failed | Retried |
|---|---|---|---|---|---|---|---|---|
| 1 | 94 (100 pages) | 69.70 s | 80.9 | 86.1 | 0.14 s / 0.25 s | 1.72 s / 1.95 s | 0 (0%) | 0 |
| 4 | 94 (100 pages) | 28.94 s | 194.9 | 207.3 | 0.30 s / 1.13 s | 1.86 s / 2.25 s | 0 (0%) | 0 |

## Pipeline stages (1 claim loop)

| Stage | Native p50 | Native p95 | Scanned p50 | Scanned p95 |
|---|---|---|---|---|
| integrity | 1.34 ms | 1.78 ms | 1.82 ms | 3.08 ms |
| inspect | 3.03 ms | 6.73 ms | 1.09 ms | 12.75 ms |
| extract | 51.92 ms | 107.86 ms | 1,626.46 ms | 1,857.51 ms |
| previews | 2.38 ms | 4.56 ms | 3.39 ms | 4.16 ms |
| classify | 9.42 ms | 16.02 ms | 9.71 ms | 12.93 ms |
| fields | 13.87 ms | 52.15 ms | 13.38 ms | 19.52 ms |
| index | 2.77 ms | 7.29 ms | 3.17 ms | 3.85 ms |

## API latency (in process)

| Request | Samples | p50 | p95 | Throughput |
|---|---|---|---|---|
| Inbox, 25 documents (GET /documents) | 30 | 24.65 ms | 30.64 ms |  |
| Document detail | 30 | 19.42 ms | 22.60 ms |  |
| Extracted fields with evidence | 30 | 17.93 ms | 22.21 ms |  |
| Checks (findings) | 30 | 34.54 ms | 39.43 ms |  |
| Review queue | 30 | 25.43 ms | 32.91 ms |  |
| Dashboard summary, 30 days | 30 | 43.14 ms | 50.61 ms |  |
| Document search (structured + text) | 30 | 27.51 ms | 33.78 ms |  |
| Inbox, 8 concurrent clients | 80 | 121.28 ms | 693.39 ms | 44.7 req/s |

## Upload (validation, storage, database; in process)

| Samples | p50 | p95 |
|---|---|---|
| 94 | 28.43 ms | 37.86 ms |

## Notes

* Throughput is measured with the whole dataset queued at once, from the first claim to the last finish: the worker is never idle. Processing time is claim to finish of each document's job and includes matching, rules and the review queue.
* Scanned documents are OCR'd page by page with Tesseract; native PDFs use their text layer. More claim loops in one process overlap I/O and Tesseract subprocesses; PDF rendering is serialized per process, so CPU-bound work scales with more worker processes, which this suite does not run.
* API numbers are in process (the application and its database queries); a deployment adds the network, nginx and TLS. Authentication is a bearer token; the dashboard and search read the processed dataset.
* No LLM is configured: 0 model calls. Model latency and cost per document are not measured here.
* These numbers describe this machine (see Provenance) and this dataset; they are a baseline for regressions, not a capacity promise.

## Provenance

```json
{
  "dataset": {
    "name": "synthetic-core + light re-scans",
    "seed": 23,
    "bundles_per_scenario": 2,
    "scenarios": "all",
    "documents": 94,
    "native": 70,
    "scanned": 24
  },
  "config": {
    "mode": "deterministic (no LLM)",
    "worker": "one process, Worker.run with N claim loops (WORKER_CONCURRENCY)",
    "claim_loops": [
      1,
      4
    ],
    "ocr_concurrency": 2,
    "api": "in process: httpx ASGITransport, no network, nginx or TLS",
    "api_repeats": 30,
    "seconds": 119.7
  },
  "environment": {
    "python": "3.13.16",
    "pypdfium2": "5.14.0",
    "pillow": "12.3.0",
    "scikit-learn": "1.9.1",
    "numpy": "2.5.3",
    "rapidfuzz": "3.14.6",
    "reportlab": "5.0.1",
    "tesseract": "tesseract 5.3.4",
    "postgresql": "17.11 (Debian 17.11-1.pgdg12+2)",
    "cpus": "4",
    "machine": "x86_64",
    "platform": "Linux-6.18.44-fc-v114-x86_64-with-glibc2.39"
  }
}
```
