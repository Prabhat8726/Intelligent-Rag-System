"""Clause segmentation and version comparison (Module 27)."""

from __future__ import annotations

import random

from docintel.synthetic.contracts import contract_family
from docintel.versions.clauses import ClauseChange, compare_clauses, segment, summarize

V1 = """MASTER SERVICES AGREEMENT
Contract No. AGR-2026-0001
This Agreement is entered into by Kestrel and Meridian.
1. Definitions
In this Agreement capitalised terms have the meanings set out in this clause.
2. Term and Termination
This Agreement continues for an initial term of 24 months. Either Party may terminate
this Agreement upon 30 days written notice.
3. Confidentiality
Each Party shall keep confidential all Confidential Information of the other Party.
4. Governing Law
This Agreement shall be governed by the laws of Germany.
IN WITNESS WHEREOF the Parties have executed this Agreement.
For Kestrel: ______
"""
# Termination notice changed, Confidentiality removed, Insurance added (clauses renumbered).
V2 = """MASTER SERVICES AGREEMENT
Contract No. AGR-2026-0001
This Agreement is entered into by Kestrel and Meridian.
1. Definitions
In this Agreement capitalised terms have the meanings set out in this clause.
2. Term and Termination
This Agreement continues for an initial term of 24 months. Either Party may terminate
this Agreement upon 90 days written notice.
3. Insurance
The Supplier shall maintain professional liability insurance.
4. Governing Law
This Agreement shall be governed by the laws of Germany.
IN WITNESS WHEREOF the Parties have executed this Agreement.
For Kestrel: ______
"""


def test_segments_preamble_clauses_and_signatures() -> None:
    clauses = segment([V1])
    assert [(c.key, c.title) for c in clauses] == [
        ("preamble", "Preamble"),
        ("1", "Definitions"),
        ("2", "Term and Termination"),
        ("3", "Confidentiality"),
        ("4", "Governing Law"),
        ("signatures", "Signatures"),
    ]
    assert "30 days written notice" in " ".join(clauses[2].text.split())
    assert clauses[5].text.startswith("IN WITNESS WHEREOF")


def test_clauses_span_pages_and_wrapped_lines() -> None:
    page_one, page_two = V1.split("3. Confidentiality")
    clauses = segment([page_one, "3. Confidentiality" + page_two])
    assert [c.title for c in clauses][3] == "Confidentiality"
    assert clauses[3].page == 2


def test_added_removed_and_modified_clauses() -> None:
    diffs = compare_clauses(segment([V1]), segment([V2]))
    changes = {(d.change, d.title) for d in diffs}
    assert (ClauseChange.MODIFIED, "Term and Termination") in changes
    assert (ClauseChange.REMOVED, "Confidentiality") in changes
    assert (ClauseChange.ADDED, "Insurance") in changes
    assert (ClauseChange.UNCHANGED, "Governing Law") in changes
    assert summarize(diffs) == {"UNCHANGED": 4, "MODIFIED": 1, "ADDED": 1, "REMOVED": 1}
    modified = next(d for d in diffs if d.change == ClauseChange.MODIFIED)
    assert {"op": "replace", "old": "30", "new": "90"} in modified.operations
    assert modified.similarity < 100
    # Order follows the new version; a removed clause appears before the next surviving one.
    assert [d.title for d in diffs][2:6] == [
        "Term and Termination",
        "Insurance",
        "Confidentiality",
        "Governing Law",
    ]


def test_renumbered_clauses_are_unchanged_not_replaced() -> None:
    v2 = V1.replace("1. Definitions", "1. Scope\nThis Agreement covers services.\n2. Definitions")
    v2 = v2.replace("2. Term", "3. Term").replace("3. Confidentiality", "4. Confidentiality")
    v2 = v2.replace("4. Governing", "5. Governing")
    diffs = compare_clauses(segment([V1]), segment([v2]))
    governing = next(d for d in diffs if d.title == "Governing Law")
    assert governing.change == ClauseChange.UNCHANGED
    assert governing.renumbered
    assert [d.title for d in diffs if d.change == ClauseChange.ADDED] == ["Scope"]


def test_generated_contract_families_record_every_change() -> None:
    rng = random.Random(9)
    for index in range(1, 6):
        versions, changes = contract_family(rng, index)
        assert [v.number for v in versions] == [1, 2, 3]
        assert len(changes) == 2
        for step, change in enumerate(changes):
            assert any(change.values())
            old_titles = {title for title, _ in versions[step].clauses}
            new_titles = {title for title, _ in versions[step + 1].clauses}
            assert set(change["added"]) == new_titles - old_titles
            assert set(change["removed"]) == old_titles - new_titles
            for title in change["modified"]:
                before = dict(versions[step].clauses)[title]
                after = dict(versions[step + 1].clauses)[title]
                assert before != after
