"""Worker liveness probe: is the heartbeat file fresh?

Kept free of the processing, agent and workflow imports so that a container health check
(`python -m docintel.workers.health`) answers in a fraction of a second even on a busy host;
`docintel worker-health` runs the same check.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from docintel.core.config import Settings, get_settings


def heartbeat_age_seconds(path: Path) -> float | None:
    """Seconds since the worker last touched its heartbeat file (None if missing)."""
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None


def heartbeat_limit_seconds(settings: Settings) -> float:
    """A healthy worker touches the file at least once per poll interval or lease heartbeat."""
    return max(60.0, settings.worker_poll_interval_seconds * 3, settings.job_lease_seconds / 2)


def check(settings: Settings) -> str | None:
    """None when the worker is alive, otherwise why it is not."""
    path = settings.worker_heartbeat_file
    if path is None:
        return "WORKER_HEARTBEAT_FILE is not configured"
    age = heartbeat_age_seconds(path)
    limit = heartbeat_limit_seconds(settings)
    if age is None or age > limit:
        return f"worker heartbeat is stale (age={age}, limit={limit})"
    return None


def main() -> int:
    problem = check(get_settings())
    if problem is not None:
        sys.stderr.write(f"[FAIL] {problem}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
