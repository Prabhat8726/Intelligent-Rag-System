# Load test 2026-10-10T19:33:27+00:00

Target http://127.0.0.1:8091 (api_replicas=2, workers=1, worker_concurrency=1, phase=mixed); 8 virtual users for 60 s after a 5 s warm-up; load generator on 4 CPUs (Linux-6.18.44-fc-v114-x86_64-with-glibc2.39).

| Endpoint | Requests | p50 ms | p95 ms | p99 ms | max ms |
|---|---:|---:|---:|---:|---:|
| inbox | 729 | 89.3 | 183.2 | 316.3 | 373.7 |
| document | 728 | 80.9 | 147.4 | 220.9 | 357.5 |
| extraction | 728 | 84.0 | 166.3 | 336.6 | 377.9 |
| findings | 729 | 117.0 | 207.7 | 285.5 | 437.6 |
| review_queue | 728 | 93.2 | 168.2 | 233.2 | 380.5 |
| dashboard | 726 | 157.7 | 277.9 | 394.1 | 433.0 |
| all | 4368 | 100.5 | 213.8 | 325.7 | 437.6 |

Throughput 72.8 successful reads/s; status codes {'200': 4368}; transport errors none.

Processing during the run: 131 documents uploaded (105 native, 26 scanned); native p50/p95 0.195/0.358 s per document, scanned p50/p95 2.029/2.306 s per page; 0 failed, 0 unfinished.

| NFR-09 target | Target | Measured | Met |
|---|---:|---:|---|
| read_p95_ms | 300.0 | 213.8 | yes |
| native_document_p95_s | 1.0 | 0.358 | yes |
| scanned_page_p95_s | 5.0 | 2.306 | yes |
| no_failed_jobs | - | - | yes |
