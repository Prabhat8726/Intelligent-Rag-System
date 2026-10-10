# Load test 2026-10-10T19:27:06+00:00

Target http://127.0.0.1:8091 (api_replicas=1, workers=1, phase=reads); 8 virtual users for 60 s after a 5 s warm-up; load generator on 4 CPUs (Linux-6.18.44-fc-v114-x86_64-with-glibc2.39).

| Endpoint | Requests | p50 ms | p95 ms | p99 ms | max ms |
|---|---:|---:|---:|---:|---:|
| inbox | 578 | 131.5 | 171.5 | 342.0 | 403.3 |
| document | 580 | 108.9 | 144.5 | 171.8 | 386.7 |
| extraction | 582 | 107.8 | 151.3 | 303.5 | 354.1 |
| findings | 581 | 140.1 | 191.8 | 354.6 | 389.4 |
| review_queue | 582 | 117.3 | 165.4 | 313.9 | 394.6 |
| dashboard | 580 | 184.4 | 242.6 | 386.2 | 463.5 |
| all | 3483 | 127.1 | 207.0 | 330.3 | 463.5 |

Throughput 58.0 successful reads/s; status codes {'200': 3483}; transport errors none.

| NFR-09 target | Target | Measured | Met |
|---|---:|---:|---|
| read_p95_ms | 300.0 | 207.0 | yes |
