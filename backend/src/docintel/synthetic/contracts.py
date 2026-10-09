"""Synthetic contracts in successive versions (v1, v2, v3) with recorded clause changes.

Each new version applies one to three edits to the previous one - a modified clause (a changed
number, period or amount, or an added sentence), an added clause, a removed clause - and
renumbers the clauses as a real redline would. The ground truth lists the titles added,
removed and modified per version step (Module 27, Module 34).
"""

from __future__ import annotations

import io
import json
import random
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer

from docintel.synthetic.catalog import BUYER_NAME, VENDORS

CLAUSE_LIBRARY: tuple[tuple[str, str], ...] = (
    (
        "Definitions",
        "In this Agreement capitalised terms have the meanings set out in this clause.",
    ),
    (
        "Term and Termination",
        "This Agreement commences on the Effective Date and continues for an initial term of "
        "{term} months. Either Party may terminate this Agreement upon {notice} days written "
        "notice.",
    ),
    (
        "Services",
        "The Supplier shall provide the Services described in Schedule A with reasonable skill "
        "and care and in accordance with good industry practice.",
    ),
    (
        "Fees and Payment",
        "The Customer shall pay the fees set out in Schedule B within {payment} days of receipt "
        "of a valid invoice. Late payments bear interest at {interest} percent per annum.",
    ),
    (
        "Confidentiality",
        "Each Party shall keep confidential all Confidential Information of the other Party and "
        "shall not disclose it to any third party without prior written consent.",
    ),
    (
        "Limitation of Liability",
        "Neither Party shall be liable for any indirect or consequential loss. The total "
        "liability of each Party shall not exceed {cap} {currency}.",
    ),
    (
        "Indemnification",
        "The Supplier shall indemnify the Customer against all claims arising from a breach of "
        "this Agreement by the Supplier.",
    ),
    (
        "Warranties",
        "The Supplier warrants that the Services will be performed by suitably qualified "
        "personnel and that deliverables will be free from material defects for {warranty} "
        "months.",
    ),
    (
        "Data Protection",
        "Each Party shall comply with applicable data protection laws and shall process "
        "personal data only on documented instructions.",
    ),
    (
        "Insurance",
        "The Supplier shall maintain professional liability insurance with a limit of at least "
        "{insurance} {currency} per claim.",
    ),
    (
        "Audit Rights",
        "The Customer may audit the Supplier's records relating to this Agreement once per "
        "year on {audit_notice} days notice.",
    ),
    (
        "Subcontracting",
        "The Supplier shall not subcontract any of its obligations without the prior written "
        "consent of the Customer.",
    ),
    (
        "Non-Solicitation",
        "During the term and for {solicit} months after, neither Party shall solicit the "
        "employees of the other Party.",
    ),
    (
        "Force Majeure",
        "Neither Party shall be in breach of this Agreement if it is prevented from performing "
        "by events beyond its reasonable control.",
    ),
    (
        "Dispute Resolution",
        "The Parties shall attempt to resolve any dispute by negotiation for {negotiation} days "
        "before starting proceedings.",
    ),
    (
        "Governing Law",
        "This Agreement shall be governed by and construed in accordance with the laws of {law}.",
    ),
    (
        "Assignment",
        "Neither Party may assign this Agreement without the prior written consent of the "
        "other Party.",
    ),
    (
        "Notices",
        "Notices under this Agreement must be in writing and delivered to the addresses stated "
        "above.",
    ),
)
ADDED_SENTENCES = (
    "This obligation survives the termination of this Agreement.",
    "The Customer may review this provision annually.",
    "Any change to this clause requires a written amendment signed by both Parties.",
)
_NUMBER = re.compile(r"(?<![\d,])\d+(?![\d,])")  # whole numbers, not "500" of "500,000"
_LAWS = ("England and Wales", "the State of Delaware", "Germany", "the Netherlands", "India")


@dataclass(slots=True)
class ContractVersion:
    number: int
    title: str
    contract_number: str
    supplier: str
    effective: date
    expiration: date
    clauses: list[tuple[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.number,
            "contract_number": self.contract_number,
            "effective_date": self.effective.isoformat(),
            "expiration_date": self.expiration.isoformat(),
            "clauses": [{"number": i + 1, "title": t} for i, (t, _) in enumerate(self.clauses)],
        }


def _values(rng: random.Random) -> dict[str, Any]:
    return {
        "term": rng.choice((12, 24, 36)),
        "notice": rng.choice((30, 60, 90)),
        "payment": rng.choice((30, 45, 60)),
        "interest": rng.choice((2, 4, 8)),
        "cap": rng.choice(("250,000", "500,000", "1,000,000")),
        "currency": rng.choice(("USD", "EUR", "GBP")),
        "warranty": rng.choice((6, 12)),
        "insurance": rng.choice(("1,000,000", "2,000,000")),
        "audit_notice": rng.choice((10, 15, 30)),
        "solicit": rng.choice((6, 12)),
        "negotiation": rng.choice((20, 30)),
        "law": rng.choice(_LAWS),
    }


def _modify(body: str, rng: random.Random) -> str:
    """A visible change a reviewer must notice: a different number, or an added sentence."""
    numbers = list(_NUMBER.finditer(body))
    if numbers and rng.random() < 0.7:
        target = rng.choice(numbers)
        value = int(target.group())
        replacement = str(value * 2 if value < 500 else value + 15)
        return body[: target.start()] + replacement + body[target.end() :]
    return f"{body} {rng.choice(ADDED_SENTENCES)}"


def contract_family(
    rng: random.Random, index: int, versions: int = 3
) -> tuple[list[ContractVersion], list[dict[str, list[str]]]]:
    """Versions of one contract and, per step (v1->v2, v2->v3), what changed."""
    values = _values(rng)
    library = list(CLAUSE_LIBRARY)
    chosen = sorted(rng.sample(range(len(library)), rng.randint(6, 9)))
    effective = date(2026, 1, 1) + timedelta(days=rng.randint(0, 200))
    first = ContractVersion(
        number=1,
        title=rng.choice(("Master Services Agreement", "Supply Agreement", "Consulting Agreement")),
        contract_number=f"AGR-2026-{index:04d}",
        supplier=rng.choice(VENDORS).name,
        effective=effective,
        expiration=effective + timedelta(days=rng.choice((365, 730, 1095))),
        clauses=[(library[i][0], library[i][1].format(**values)) for i in chosen],
    )
    family = [first]
    changes: list[dict[str, list[str]]] = []
    unused = [i for i in range(len(library)) if i not in chosen]
    for number in range(2, versions + 1):
        previous = family[-1]
        clauses = list(previous.clauses)
        step: dict[str, list[str]] = {"added": [], "removed": [], "modified": []}
        edits = rng.sample(("modify", "add", "remove", "modify"), rng.randint(1, 3))
        touched: set[str] = set()
        for edit in edits:
            candidates = [c for c in clauses if c[0] not in touched and c[0] != "Definitions"]
            if edit == "modify" and candidates:
                title, body = rng.choice(candidates)
                position = clauses.index((title, body))
                clauses[position] = (title, _modify(body, rng))
                step["modified"].append(title)
                touched.add(title)
            elif edit == "add" and unused:
                pick = unused.pop(rng.randrange(len(unused)))
                title, template = library[pick]
                clauses.insert(rng.randint(1, len(clauses)), (title, template.format(**values)))
                step["added"].append(title)
                touched.add(title)
            elif edit == "remove" and len(candidates) > 4:
                title, body = rng.choice(candidates)
                clauses.remove((title, body))
                step["removed"].append(title)
                touched.add(title)
        if not any(step.values()):  # every version changes something
            title, body = clauses[-1]
            clauses[-1] = (title, _modify(body, rng))
            step["modified"].append(title)
        family.append(
            ContractVersion(
                number=number,
                title=previous.title,
                contract_number=previous.contract_number,
                supplier=previous.supplier,
                effective=previous.effective,
                expiration=previous.expiration
                + (timedelta(days=365) if rng.random() < 0.3 else timedelta(0)),
                clauses=clauses,
            )
        )
        changes.append(step)
    return family, changes


def render_contract_pdf(version: ContractVersion) -> bytes:
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=13)
    heading = ParagraphStyle(
        "clause", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=10.5, leading=14
    )
    title = ParagraphStyle("title", parent=styles["Heading1"], fontSize=16)
    story: list[Flowable] = [
        Paragraph(escape(version.title.upper()), title),
        Paragraph(f"Contract No. {version.contract_number}", body),
        Paragraph(f"Version {version.number}", body),
        Paragraph(f"Effective Date: {version.effective.isoformat()}", body),
        Paragraph(f"Expiration Date: {version.expiration.isoformat()}", body),
        Spacer(1, 4 * mm),
        Paragraph(
            escape(
                f"This Agreement is entered into by and between {version.supplier} (the "
                f'"Supplier") and {BUYER_NAME} (the "Customer").'
            ),
            body,
        ),
        Spacer(1, 3 * mm),
    ]
    for number, (clause_title, text) in enumerate(version.clauses, start=1):
        story.append(Paragraph(escape(f"{number}. {clause_title}"), heading))
        story.append(Paragraph(escape(text), body))
        story.append(Spacer(1, 2 * mm))
    story.append(Spacer(1, 4 * mm))
    story.append(
        Paragraph(
            "IN WITNESS WHEREOF the Parties have executed this Agreement as of the Effective Date.",
            body,
        )
    )
    story.append(Paragraph(escape(f"For {version.supplier}: ____________________"), body))
    story.append(Paragraph(escape(f"For {BUYER_NAME}: ____________________"), body))
    story.append(
        Paragraph("SYNTHETIC DOCUMENT - generated for testing; not a real contract.", body)
    )
    buffer = io.BytesIO()
    SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm, invariant=1
    ).build(story)
    return buffer.getvalue()


def generate_contract_versions(output: Path, *, seed: int, families: int) -> dict[str, Any]:
    """Write families of contract versions as PDFs with a JSON ground truth per family."""
    rng = random.Random(seed)  # noqa: S311  (reproducible test data, not security)
    output.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for index in range(1, families + 1):
        versions, changes = contract_family(rng, index)
        files = []
        for version in versions:
            name = f"C{index:04d}-v{version.number}.pdf"
            (output / name).write_bytes(render_contract_pdf(version))
            files.append(name)
        truth = {
            "family": f"C{index:04d}",
            "files": files,
            "versions": [version.to_json() for version in versions],
            "changes": changes,
        }
        (output / f"C{index:04d}.json").write_text(json.dumps(truth, indent=2))
        entries.append(truth)
    manifest = {"seed": seed, "families": families, "contracts": entries}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
