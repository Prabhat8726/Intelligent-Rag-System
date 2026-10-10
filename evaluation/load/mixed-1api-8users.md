# Load test 2026-10-10T19:36:05+00:00

Target http://127.0.0.1:8091 (api_replicas=1, workers=1, worker_concurrency=1, phase=mixed); 8 virtual users for 60 s after a 5 s warm-up; load generator on 4 CPUs (Linux-6.18.44-fc-v114-x86_64-with-glibc2.39).

| Endpoint | Requests | p50 ms | p95 ms | p99 ms | max ms |
|---|---:|---:|---:|---:|---:|
| inbox | 497 | 137.4 | 189.5 | 362.9 | 385.7 |
| document | 499 | 129.8 | 175.4 | 328.3 | 377.1 |
| extraction | 497 | 128.8 | 169.6 | 197.9 | 350.4 |
| findings | 497 | 187.7 | 242.6 | 399.8 | 425.3 |
| review_queue | 497 | 125.2 | 175.1 | 318.1 | 354.6 |
| dashboard | 497 | 218.9 | 280.4 | 428.4 | 467.0 |
| all | 2984 | 145.5 | 245.2 | 354.6 | 467.0 |

Throughput 49.7 successful reads/s; status codes {'200': 2984}; transport errors none.

Processing during the run: 131 documents uploaded (105 native, 26 scanned); native p50/p95 0.156/0.271 s per document, scanned p50/p95 1.988/2.225 s per page; 0 failed, 0 unfinished.

| NFR-09 target | Target | Measured | Met |
|---|---:|---:|---|
| read_p95_ms | 300.0 | 245.2 | yes |
| native_document_p95_s | 1.0 | 0.271 | yes |
| scanned_page_p95_s | 5.0 | 2.225 | yes |
| no_failed_jobs | - | - | yes |
