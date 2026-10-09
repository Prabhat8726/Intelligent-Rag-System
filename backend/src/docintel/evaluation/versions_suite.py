"""Versions suite: clause-level comparison of contract versions against ground truth (Module 27).

Synthetic contract families (v1, v2, v3) record which clauses each step added, removed or
modified; clauses are renumbered as a real redline would. Every version is extracted exactly
as the worker does (text layer or OCR), segmented into clauses from its page texts and
compared step by step (v1->v2, v2->v3) with `versions.clauses.compare_clauses`, the function
behind GET /documents/{id}/versions/compare.

Inputs: native PDFs and the same PDFs re-rendered as light scans (OCR).
Measured: clause segmentation (count and titles per version), precision / recall / F1 per
change type, and the share of steps whose change list is exactly right. The preamble and the
signature block are not clauses (the version number and dates in the preamble change every
step) and are left out of the scores.
"""

from __future__ import annotations

import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from docintel.documents.validation import FileKind
from docintel.evaluation.common import DEFAULT_PARALLELISM, extract_file, run_bounded, scan_pdf
from docintel.evaluation.metrics import counts_prf
from docintel.evaluation.report import Report, environment, pct
from docintel.processing.extraction import ExtractionOptions
from docintel.processing.ocr import TesseractOCRProvider
from docintel.synthetic.contracts import generate_contract_versions
from docintel.versions.clauses import Clause, ClauseChange, compare_clauses, segment

DATASET_SEED = 61
TITLE_MATCH = 90.0  # titles read by OCR may differ by a character
NOT_CLAUSES = frozenset({"preamble", "signatures"})
CHANGES = {
    "added": ClauseChange.ADDED,
    "removed": ClauseChange.REMOVED,
    "modified": ClauseChange.MODIFIED,
}


def same_title(found: str, expected: str) -> bool:
    return fuzz.ratio(found.casefold(), expected.casefold()) >= TITLE_MATCH


def _matched(found: list[str], expected: list[str]) -> int:
    """Pairs of equal titles (each expected title used once)."""
    remaining = list(expected)
    hits = 0
    for title in found:
        match = next((item for item in remaining if same_title(title, item)), None)
        if match is not None:
            remaining.remove(match)
            hits += 1
    return hits


def score_step(
    old: list[Clause], new: list[Clause], truth: dict[str, list[str]]
) -> dict[str, dict[str, int]]:
    """tp / fp / fn per change type for one version step."""
    found: dict[str, list[str]] = defaultdict(list)
    for diff in compare_clauses(old, new):
        clause = diff.new or diff.old
        if clause is None or clause.key in NOT_CLAUSES:
            continue
        for name, change in CHANGES.items():
            if diff.change == change:
                found[name].append(diff.title)
    scores: dict[str, dict[str, int]] = {}
    for name in CHANGES:
        expected = truth.get(name, [])
        hits = _matched(found[name], expected)
        scores[name] = {"tp": hits, "fp": len(found[name]) - hits, "fn": len(expected) - hits}
    return scores


def score_segmentation(clauses: list[Clause], truth: dict[str, Any]) -> dict[str, bool]:
    titles = [clause.title for clause in clauses if clause.key not in NOT_CLAUSES]
    expected = [clause["title"] for clause in truth["clauses"]]
    return {
        "count": len(titles) == len(expected),
        "titles": len(titles) == len(expected)
        and all(same_title(a, b) for a, b in zip(titles, expected, strict=True)),
    }


async def run_versions_suite(
    output: Path, *, quick: bool = False, languages: str = "eng"
) -> Report:
    ocr = TesseractOCRProvider(languages=languages, timeout_seconds=180)
    version = await ocr.verify()
    options = ExtractionOptions()
    families = 4 if quick else 20
    with tempfile.TemporaryDirectory(prefix="docintel-versions-eval-") as tmp:
        root = Path(tmp)
        manifest = generate_contract_versions(root, seed=DATASET_SEED, families=families)
        jobs: list[tuple[str, str, int, Path]] = []
        for contract in manifest["contracts"]:
            for number, name in enumerate(contract["files"], start=1):
                path = root / name
                jobs.append(("native", contract["family"], number, path))
                scanned = path.with_name(path.stem + "-scan.pdf")
                scanned.write_bytes(scan_pdf(path.read_bytes(), seed=len(jobs)))
                jobs.append(("scanned (re-rendered)", contract["family"], number, scanned))

        async def extract(
            job: tuple[str, str, int, Path],
        ) -> tuple[tuple[str, str, int], list[Clause]]:
            method, family, number, path = job
            pages = await extract_file(path, FileKind.PDF, ocr, options)
            return (method, family, number), segment([page.text for page in pages])

        clauses = dict(await run_bounded(jobs, extract, DEFAULT_PARALLELISM))

    methods = sorted({method for method, _, _ in clauses})
    metrics: dict[str, Any] = {}
    failures: list[list[str]] = []
    for method in methods:
        totals: dict[str, dict[str, int]] = {name: {"tp": 0, "fp": 0, "fn": 0} for name in CHANGES}
        exact: list[bool] = []
        segmentation: dict[str, list[bool]] = {"count": [], "titles": []}
        for contract in manifest["contracts"]:
            family = contract["family"]
            for number, truth in enumerate(contract["versions"], start=1):
                for key, ok in score_segmentation(clauses[method, family, number], truth).items():
                    segmentation[key].append(ok)
            for step, truth in enumerate(contract["changes"], start=1):
                scores = score_step(
                    clauses[method, family, step], clauses[method, family, step + 1], truth
                )
                for name, counts in scores.items():
                    for key, value in counts.items():
                        totals[name][key] += value
                right = all(counts["fp"] == 0 and counts["fn"] == 0 for counts in scores.values())
                exact.append(right)
                if not right:
                    failures.append(
                        [
                            method,
                            f"{family} v{step}->v{step + 1}",
                            "; ".join(
                                f"{name}: +{counts['fp']} wrong, {counts['fn']} missed"
                                for name, counts in scores.items()
                                if counts["fp"] or counts["fn"]
                            ),
                        ]
                    )
        overall = {
            key: sum(counts[key] for counts in totals.values()) for key in ("tp", "fp", "fn")
        }
        metrics[method] = {
            "steps": len(exact),
            "steps_exact": round(sum(exact) / len(exact), 4),
            "by_change": {name: counts_prf(**counts) for name, counts in totals.items()},
            "overall": counts_prf(**overall),
            "segmentation": {
                key: {"versions": len(values), "rate": round(sum(values) / len(values), 4)}
                for key, values in segmentation.items()
            },
        }

    report = Report(
        suite="versions",
        title="Contract version comparison (clause changes)",
        dataset={
            "name": "synthetic-contract-versions",
            "seed": DATASET_SEED,
            "families": families,
            "versions_per_family": 3,
            "steps": sum(len(contract["changes"]) for contract in manifest["contracts"]),
            "quick": quick,
        },
        config={"title_match_ratio": TITLE_MATCH, "ocr_languages": languages},
        metrics=metrics,
        environment=environment({"tesseract": version}),
    )
    report.tables.append(
        (
            "Summary",
            [
                "Input",
                "Steps",
                "Steps exactly right",
                "Change P / R",
                "Added P / R",
                "Removed P / R",
                "Modified P / R",
                "Segmentation (titles)",
            ],
            [
                [
                    method,
                    str(stats["steps"]),
                    pct(stats["steps_exact"]),
                    *(
                        f"{pct(part['precision'])} / {pct(part['recall'])}"
                        for part in (
                            stats["overall"],
                            *(stats["by_change"][name] for name in CHANGES),
                        )
                    ),
                    pct(stats["segmentation"]["titles"]["rate"]),
                ]
                for method, stats in metrics.items()
            ],
        )
    )
    if failures:
        report.tables.append(("Steps not exactly right", ["Input", "Step", "Errors"], failures))
    report.notes += [
        "A step is exactly right when its added, removed and modified clauses are all found "
        "and nothing else is reported (renumbering alone is not a change).",
        f"Titles are compared case-insensitively with a similarity of at least {TITLE_MATCH:.0f}"
        " (an OCR slip in a heading does not hide a correctly detected change).",
        "Synthetic contracts use one heading style ('N. Title'); other styles (Clause N, "
        "Article N, nested N.N) are covered by unit tests, not measured here.",
    ]
    report.write(output)
    return report
