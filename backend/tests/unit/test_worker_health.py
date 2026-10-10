"""The worker's container probe: heartbeat freshness, and a module light enough to answer fast."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from docintel import cli
from docintel.workers.health import check, heartbeat_limit_seconds
from tests.conftest import make_settings


def test_a_fresh_heartbeat_is_healthy_and_a_stale_or_missing_one_is_not(tmp_path: Path) -> None:
    heartbeat = tmp_path / "worker.heartbeat"
    settings = make_settings(worker_heartbeat_file=heartbeat)
    assert check(make_settings(worker_heartbeat_file=None)) == (
        "WORKER_HEARTBEAT_FILE is not configured"
    )
    missing = check(settings)
    assert missing is not None
    assert "stale (age=None" in missing

    heartbeat.touch()
    assert check(settings) is None
    old = time.time() - heartbeat_limit_seconds(settings) - 5
    os.utime(heartbeat, (old, old))
    stale = check(settings)
    assert stale is not None
    assert "stale" in stale


def test_the_cli_command_runs_the_same_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = tmp_path / "worker.heartbeat"
    heartbeat.touch()
    settings = make_settings(worker_heartbeat_file=heartbeat)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["worker-health"]) == 0
    heartbeat.unlink()
    assert cli.main(["worker-health"]) == 1


def test_the_probe_does_not_import_the_processing_or_agent_stack() -> None:
    # The container health check runs `python -m docintel.workers.health` every 15 s with a
    # 5 s timeout; importing scikit-learn or LangGraph alone takes seconds on a busy host.
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, docintel.workers.health; "
            "print(sorted(m for m in ('sklearn', 'langgraph', 'docintel.workers.runner', "
            "'docintel.processing.services') if m in sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert loaded.stdout.strip() == "[]"
