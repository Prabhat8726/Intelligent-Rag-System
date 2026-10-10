# Load test 2026-10-10T19:30:58+00:00

Target http://127.0.0.1:8091 (api_replicas=3, workers=1, phase=reads); 16 virtual users for 60 s after a 5 s warm-up; load generator on 4 CPUs (Linux-6.18.44-fc-v114-x86_64-with-glibc2.39).

| Endpoint | Requests | p50 ms | p95 ms | p99 ms | max ms |
|---|---:|---:|---:|---:|---:|
| inbox | 1106 | 120.8 | 300.2 | 441.7 | 592.4 |
| document | 1106 | 105.0 | 250.5 | 382.0 | 548.0 |
| extraction | 1108 | 103.5 | 253.1 | 413.6 | 537.5 |
| findings | 1105 | 143.5 | 322.9 | 463.4 | 614.6 |
| review_queue | 1107 | 110.9 | 251.0 | 378.2 | 518.4 |
| dashboard | 1108 | 187.6 | 405.7 | 556.3 | 702.4 |
| all | 6640 | 125.8 | 319.1 | 457.1 | 702.4 |

Throughput 110.7 successful reads/s; status codes {'200': 6640}; transport errors none.

| NFR-09 target | Target | Measured | Met |
|---|---:|---:|---|
| read_p95_ms | 300.0 | 319.1 | no |
