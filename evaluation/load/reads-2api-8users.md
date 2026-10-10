# Load test 2026-10-10T19:28:25+00:00

Target http://127.0.0.1:8091 (api_replicas=2, workers=1, phase=reads); 8 virtual users for 60 s after a 5 s warm-up; load generator on 4 CPUs (Linux-6.18.44-fc-v114-x86_64-with-glibc2.39).

| Endpoint | Requests | p50 ms | p95 ms | p99 ms | max ms |
|---|---:|---:|---:|---:|---:|
| inbox | 901 | 84.0 | 157.6 | 249.5 | 341.1 |
| document | 900 | 64.8 | 125.8 | 161.2 | 321.9 |
| extraction | 900 | 66.7 | 132.0 | 286.2 | 331.8 |
| findings | 899 | 96.6 | 157.0 | 219.2 | 354.6 |
| review_queue | 900 | 76.7 | 138.2 | 205.1 | 352.5 |
| dashboard | 899 | 124.4 | 215.3 | 340.9 | 427.3 |
| all | 5399 | 82.5 | 167.4 | 265.9 | 427.3 |

Throughput 90.0 successful reads/s; status codes {'200': 5399}; transport errors none.

| NFR-09 target | Target | Measured | Met |
|---|---:|---:|---|
| read_p95_ms | 300.0 | 167.4 | yes |
