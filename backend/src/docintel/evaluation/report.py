"""Evaluation reports: JSON (machine-readable) + Markdown (human-readable), with provenance.

Every report records when and from which commit it was produced, library and engine versions,
dataset seeds and the configuration, so numbers in the README can be traced to a run.
Reports are written to stable file names (<suite>.json/.md); git history keeps older runs.
"""

from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

TRACKED_PACKAGES = ("pypdfium2", "pillow", "scikit-learn", "numpy", "rapidfuzz", "reportlab")


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603  (fixed git subcommands, no shell)
            ["git", *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def git_revision() -> str | None:
    """Short commit hash, with "+dirty" when tracked files have uncommitted changes."""
    revision = _git("rev-parse", "--short=12", "HEAD")
    if not revision:
        return None
    status = _git("status", "--porcelain", "--untracked-files=no")
    return f"{revision}+dirty" if status else revision


def environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for package in TRACKED_PACKAGES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            continue
    versions.update(extra or {})
    return versions


@dataclass(slots=True)
class Report:
    suite: str
    title: str
    dataset: dict[str, Any]
    config: dict[str, Any]
    metrics: dict[str, Any]
    environment: dict[str, str]
    quick: bool = False  # small smoke-test datasets: not comparable with full runs
    notes: list[str] = field(default_factory=list)
    tables: list[tuple[str, list[str], list[list[str]]]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    git_revision: str | None = field(default_factory=git_revision)

    def to_json(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "title": self.title,
            "quick": self.quick,
            "created_at": self.created_at,
            "git_revision": self.git_revision,
            "environment": self.environment,
            "dataset": self.dataset,
            "config": self.config,
            "metrics": self.metrics,
            "notes": self.notes,
            "tables": [
                {"heading": heading, "header": header, "rows": rows}
                for heading, header, rows in self.tables
            ],
        }

    def to_markdown(self) -> str:
        lines = [
            f"# {self.title}",
            "",
            f"Generated {self.created_at} from commit `{self.git_revision or 'unknown'}` "
            f"by `docintel evaluate --suite {self.suite}{' --quick' if self.quick else ''}`. "
            "Do not edit by hand."
            + (
                " Quick datasets: a smoke test, not comparable with full runs."
                if self.quick
                else ""
            ),
            "",
        ]
        for heading, header, rows in self.tables:
            lines += [f"## {heading}", "", "| " + " | ".join(header) + " |"]
            lines.append("|" + "|".join("---" for _ in header) + "|")
            lines += ["| " + " | ".join(row) + " |" for row in rows]
            lines.append("")
        if self.notes:
            lines += ["## Notes", "", *[f"* {note}" for note in self.notes], ""]
        lines += ["## Provenance", "", "```json"]
        provenance = {
            "dataset": self.dataset,
            "config": self.config,
            "environment": self.environment,
        }
        lines += [json.dumps(provenance, indent=2), "```", ""]
        return "\n".join(lines)

    def write(self, directory: Path) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / f"{self.suite}.json"
        markdown_path = directory / f"{self.suite}.md"
        json_path.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")
        markdown_path.write_text(self.to_markdown(), encoding="utf-8")
        return json_path, markdown_path


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def num(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"
