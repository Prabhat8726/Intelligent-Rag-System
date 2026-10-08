"""Synthetic labelled corpus for document classification (all nine document types).

The classifier must not learn shortcuts such as "the first word is the type". So:
* titles vary, use synonyms and other languages, and are sometimes missing,
* the same vocabulary (amounts, dates, company names, tables) appears in several types,
* confusable pairs are deliberate: receipt vs invoice, quotation (OTHER) vs invoice/PO,
  packing slip vs PO, policy vs contract, cover letter (OTHER) vs resume,
* a share of samples gets OCR-like character noise.
All names, companies and numbers are fictional (example.com/.org addresses, XX IBAN prefix).
Deterministic for a given seed.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from docintel.db.models import DocumentType

COMPANIES = (
    "Kestrel Industrial Supply Inc.",
    "Bluepeak Office Solutions LLC",
    "Altamira Components GmbH",
    "Harbor & Pine Packaging Ltd.",
    "Sundaram Precision Tools Pvt. Ltd.",
    "Northwind Logistics Co.",
    "Meridian Manufacturing Co.",
    "Cobalt Ridge Consulting LLP",
    "Silverleaf Facilities Services",
    "Orchid Lane Software S.A.",
    "Granite Peak Engineering AG",
    "Lumen Analytics B.V.",
    "Redwood Office Interiors",
    "Tidewater Freight Partners",
    "Quillon Legal Services LLP",
)
BANKS = (
    "Example Commercial Bank",
    "First Fictional Savings",
    "Harbourline Bank plc",
    "Mittelstand Testbank AG",
    "Pinecrest Credit Union",
)
STORES = (
    "CORNER MARKET 24",
    "Daily Fresh Groceries",
    "Tech Hub Electronics",
    "Cafe Aurora",
    "BuildRight Hardware",
    "Metro Pharmacy",
    "Paper & Ink Stationers",
    "Fuel Stop #118",
)
FIRST_NAMES = (
    "Avery",
    "Jordan",
    "Priya",
    "Lukas",
    "Mei",
    "Samuel",
    "Amara",
    "Diego",
    "Elena",
    "Rahul",
    "Sofia",
    "Tomasz",
    "Hannah",
    "Kwame",
    "Noor",
    "Oliver",
    "Yuki",
    "Leila",
)
LAST_NAMES = (
    "Fischer",
    "Okafor",
    "Raman",
    "Novak",
    "Lindqvist",
    "Moreau",
    "Tanaka",
    "Haddad",
    "Silva",
    "Brennan",
    "Kowalski",
    "Mensah",
    "Iyer",
    "Schultz",
    "Delgado",
    "Park",
)
CITIES = (
    "Columbus, OH 43215",
    "Austin, TX 78701",
    "70565 Stuttgart",
    "Bristol BS1 6XN",
    "Coimbatore 641021",
    "Rotterdam 3011",
    "Lyon 69002",
    "Denver, CO 80202",
)
STREETS = (
    "500 Commerce Parkway",
    "77 Larkspur Avenue",
    "Industriestrasse 12",
    "Unit 4, Quayside",
    "Plot 22, SIDCO Estate",
    "18 Canal Street",
    "9 Rue des Tanneurs",
    "1200 Market Street",
)
PRODUCTS = (
    ("Deep groove ball bearing 6204", "pcs", 4.85),
    ("Brass ball valve DN50", "pcs", 38.40),
    ("Copy paper A4 80gsm, ream", "ream", 4.10),
    ("Toner cartridge black", "pcs", 72.50),
    ("Corrugated carton 400x300", "pcs", 0.92),
    ("Stretch film 500 mm", "roll", 15.40),
    ("Digital caliper 150 mm", "pcs", 34.90),
    ("Ergonomic office chair", "pcs", 189.00),
    ("Hydraulic hose 1/2in", "m", 9.75),
    ("Cut-resistant gloves", "pair", 6.75),
    ("Consulting services", "hour", 145.00),
    ("Software licence (annual)", "seat", 240.00),
    ("Maintenance visit", "visit", 320.00),
    ("Freight charges", "lot", 85.00),
)
RETAIL_ITEMS = (
    ("Coffee latte", 3.80),
    ("Croissant", 2.20),
    ("USB-C cable", 12.99),
    ("AA batteries 4pk", 5.49),
    ("Notebook A5", 3.25),
    ("Bottled water", 1.10),
    ("Hammer 16oz", 14.95),
    ("Paracetamol 500mg", 4.79),
    ("Diesel", 61.30),
    ("Sandwich", 6.50),
    ("Printer paper", 8.99),
    ("Screws assorted", 7.40),
)
OCR_CONFUSIONS = (
    ("l", "1"),
    ("O", "0"),
    ("rn", "m"),
    ("e", "c"),
    ("S", "5"),
    ("i", "l"),
    ("B", "8"),
    ("cl", "d"),
    ("vv", "w"),
    ("a", "o"),
)


@dataclass(frozen=True, slots=True)
class LabelledText:
    text: str
    label: DocumentType


@dataclass(slots=True)
class _Doc:
    parts: list[str] = field(default_factory=list)

    def add(self, *lines: str) -> None:
        self.parts.append("\n".join(line for line in lines if line))

    def fields(self, rng: random.Random, pairs: list[tuple[str, str]]) -> None:
        separator = rng.choice(["  ", ": ", " "])
        self.add(*[f"{key}{separator}{value}" for key, value in pairs])

    def table(self, header: list[str], rows: list[list[str]]) -> None:
        self.add(" | ".join(header), *[" | ".join(row) for row in rows])

    def text(self) -> str:
        return "\n\n".join(part for part in self.parts if part)


class _Gen:
    """Shared random helpers for one sample."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.today = date(2026, 1, 1) + timedelta(days=rng.randrange(0, 600))

    def pick[T](self, options: tuple[T, ...] | list[T]) -> T:
        return self.rng.choice(options)

    def maybe(self, probability: float) -> bool:
        return self.rng.random() < probability

    def person(self) -> str:
        return f"{self.pick(FIRST_NAMES)} {self.pick(LAST_NAMES)}"

    def email(self, name: str) -> str:
        user = name.lower().replace(" ", self.pick([".", "_", ""]))
        return f"{user}@example.{self.pick(['com', 'org', 'net'])}"

    def phone(self) -> str:
        return f"+{self.rng.randint(1, 99)} {self.rng.randint(100, 999)} {self.rng.randint(1000000, 9999999)}"

    def address(self) -> list[str]:
        return [self.pick(STREETS), self.pick(CITIES)]

    def day(self, offset: int = 0) -> str:
        value = self.today + timedelta(days=offset)
        fmt = self.pick(["%m/%d/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y"])
        return value.strftime(fmt)

    def amount(self, low: float = 1, high: float = 5000) -> str:
        value = self.rng.uniform(low, high)
        text = f"{value:,.2f}"
        if self.maybe(0.25):  # European separators
            text = text.replace(",", "X").replace(".", ",").replace("X", ".")
        return text

    def money(self, low: float = 1, high: float = 5000) -> str:
        symbol = self.pick(["$", "EUR", "€", "£", "USD", "GBP", "Rs."])
        amount = self.amount(low, high)
        return f"{symbol} {amount}" if self.maybe(0.6) else f"{amount} {symbol}"

    def number(self, prefix: str, digits: int = 6) -> str:
        return f"{prefix}{self.rng.randint(10 ** (digits - 1), 10**digits - 1)}"

    def line_items(self, *, prices: bool, count: int | None = None) -> list[list[str]]:
        rows = []
        for index in range(count or self.rng.randint(1, 8)):
            description, unit, price = self.pick(PRODUCTS)
            quantity = self.rng.randint(1, 200)
            row = [str(index + 1), self.number("SKU-", 4), description, str(quantity), unit]
            if prices:
                row += [f"{price:.2f}", f"{price * quantity:,.2f}"]
            rows.append(row)
        return rows


def _title(g: _Gen, options: list[str], omit: float = 0.12) -> str:
    if g.maybe(omit):
        return ""
    title = g.pick(options)
    return title.upper() if g.maybe(0.4) else title


# ------------------------------------------------------------------------------ business docs
def _invoice(g: _Gen) -> str:
    d = _Doc()
    vendor = g.pick(COMPANIES)
    d.add(vendor, *g.address())
    d.add(_title(g, ["Invoice", "Tax Invoice", "Commercial Invoice", "Rechnung", "Invoice / Bill"]))
    fields = [
        (
            g.pick(["Invoice No.", "Invoice Number", "Invoice #", "Rechnungsnummer", "Bill No."]),
            g.number(g.pick(["INV-", "", "RE-", "2026-"])),
        ),
        (g.pick(["Invoice Date", "Date", "Datum", "Date of issue"]), g.day()),
        (g.pick(["Due Date", "Payment due", "Fällig am"]), g.day(30)),
    ]
    if g.maybe(0.6):
        fields.append((g.pick(["PO Reference", "Your order", "PO No."]), g.number("PO-")))
    if g.maybe(0.5):
        fields.append(("Customer No.", g.number("C", 5)))
    d.fields(g.rng, fields)
    d.add(g.pick(["Bill To", "Sold To", "Customer", "Invoice to"]), g.pick(COMPANIES), *g.address())
    header = [
        "#",
        "Item",
        "Description",
        "Qty",
        "Unit",
        g.pick(["Unit Price", "Rate"]),
        g.pick(["Amount", "Line Total", "Total"]),
    ]
    d.table(header, g.line_items(prices=True))
    d.fields(
        g.rng,
        [
            ("Subtotal", g.money()),
            (g.pick(["VAT (20%)", "Sales tax", "GST 18%", "MwSt 19%", "Tax"]), g.money(1, 900)),
            (
                g.pick(["Total Due", "Amount Due", "Balance Due", "Invoice Total", "Gesamtbetrag"]),
                g.money(),
            ),
        ],
    )
    d.add(
        g.pick(
            [
                f"Please remit payment within {g.pick([14, 30, 45, 60])} days quoting the invoice number.",
                f"Bank: {g.pick(BANKS)}, IBAN XX{g.rng.randint(10, 99)} 0000 {g.rng.randint(1000, 9999)}.",
                "Payment by bank transfer. Late payments are subject to interest.",
                "Thank you for your business. Payment terms: net 30 days.",
            ]
        )
    )
    return d.text()


def _purchase_order(g: _Gen) -> str:
    d = _Doc()
    buyer = g.pick(COMPANIES)
    d.add(buyer, *g.address())
    d.add(_title(g, ["Purchase Order", "PO", "Order", "Bestellung", "Purchase Requisition Order"]))
    d.fields(
        g.rng,
        [
            (g.pick(["PO Number", "Order No.", "PO #", "Bestellnummer"]), g.number("PO-")),
            (g.pick(["Order Date", "Date", "Issued"]), g.day()),
            (g.pick(["Delivery Date", "Required by", "Deliver by", "Liefertermin"]), g.day(14)),
            (g.pick(["Buyer", "Requested by", "Contact"]), g.person()),
        ],
    )
    d.add(g.pick(["Supplier", "Vendor", "To"]), g.pick(COMPANIES), *g.address())
    d.add(g.pick(["Ship To", "Deliver To", "Delivery address"]), buyer, *g.address())
    d.table(
        ["#", "Item", "Description", "Qty", "Unit", "Unit Price", "Amount"],
        g.line_items(prices=True),
    )
    d.fields(g.rng, [(g.pick(["Order Total", "Total", "PO Total", "Net value"]), g.money())])
    d.add(
        g.pick(
            [
                "Please confirm this order and quote the PO number on all invoices and delivery notes.",
                "Our general terms and conditions of purchase apply. Partial deliveries require approval.",
                "Acknowledge receipt of this purchase order within 2 business days.",
                "Invoices without a valid PO number will be returned unpaid.",
            ]
        )
    )
    if g.maybe(0.5):
        d.add(
            f"Authorized by: {g.person()}",
            g.pick(["Procurement Manager", "Purchasing", "Head of Operations"]),
        )
    return d.text()


def _delivery_note(g: _Gen) -> str:
    d = _Doc()
    d.add(g.pick(COMPANIES), *g.address())
    d.add(
        _title(
            g,
            [
                "Delivery Note",
                "Packing Slip",
                "Dispatch Note",
                "Lieferschein",
                "Delivery Docket",
                "Shipping Note",
            ],
        )
    )
    d.fields(
        g.rng,
        [
            (
                g.pick(["Delivery Note No.", "Packing Slip #", "Dispatch No.", "DN Number"]),
                g.number("DN-"),
            ),
            (g.pick(["Delivery Date", "Ship Date", "Dispatched"]), g.day()),
            (g.pick(["Order Reference", "PO Reference", "Your order"]), g.number("PO-")),
            (
                g.pick(["Carrier", "Shipped via", "Courier"]),
                g.pick(["DHL", "UPS", "Own fleet", "Freight line"]),
            ),
        ],
    )
    if g.maybe(0.6):
        d.fields(
            g.rng, [("Tracking No.", g.number("1Z", 9)), ("Packages", str(g.rng.randint(1, 12)))]
        )
    d.add(g.pick(["Ship To", "Deliver To", "Consignee"]), g.pick(COMPANIES), *g.address())
    rows = g.line_items(prices=False)
    if g.maybe(0.5):
        d.table(
            ["#", "Item", "Description", "Qty Ordered", "Qty Shipped", "Unit"],
            [
                [r[0], r[1], r[2], r[3], str(max(0, int(r[3]) - g.rng.randint(0, 3))), r[4]]
                for r in rows
            ],
        )
    else:
        d.table(
            ["#", "Item", "Description", g.pick(["Qty Delivered", "Quantity", "Qty"]), "Unit"], rows
        )
    d.add(
        g.pick(
            [
                "Received in good condition: ____________________  Signature / Date",
                "Please check goods on receipt and report damages within 48 hours.",
                "Backordered items will be shipped separately.",
                "Goods remain our property until paid in full.",
            ]
        )
    )
    return d.text()


def _receipt(g: _Gen) -> str:
    d = _Doc()
    if g.maybe(0.75):  # point-of-sale receipt
        d.add(g.pick(STORES), g.pick(STREETS), g.pick(CITIES))
        d.add(_title(g, ["Receipt", "Sales Receipt", "Customer Copy", "Kassenbon"], omit=0.3))
        d.add(
            f"{g.day()} {g.rng.randint(7, 22):02d}:{g.rng.randint(0, 59):02d}",
            f"{g.pick(['Cashier', 'Server', 'Till'])}: {g.pick(FIRST_NAMES)}  "
            f"{g.pick(['Terminal', 'Reg', 'POS'])} {g.rng.randint(1, 9)}",
        )
        lines = []
        for _ in range(g.rng.randint(1, 9)):
            name, price = g.pick(RETAIL_ITEMS)
            quantity = g.rng.randint(1, 3)
            lines.append(f"{quantity} x {name}  {price * quantity:.2f}")
        d.add(*lines)
        total = g.amount(2, 300)
        d.fields(
            g.rng,
            [
                ("SUBTOTAL", g.amount(2, 300)),
                (g.pick(["TAX", "VAT", "MwSt"]), g.amount(0, 40)),
                ("TOTAL", total),
            ],
        )
        method = g.pick(
            [
                "CASH",
                "VISA ****" + str(g.rng.randint(1000, 9999)),
                "MASTERCARD ****" + str(g.rng.randint(1000, 9999)),
                "DEBIT CARD",
                "CONTACTLESS",
            ]
        )
        d.add(
            f"{method}  {total}",
            "CHANGE  0.00" if "CASH" not in method else f"CHANGE  {g.amount(0, 20)}",
        )
        d.add(
            g.pick(
                [
                    "Thank you for shopping with us!",
                    "Please keep your receipt for returns.",
                    "Returns accepted within 30 days with receipt.",
                    "Have a nice day!",
                ]
            ),
            f"Transaction {g.number('', 8)}",
        )
    else:  # payment receipt
        d.add(g.pick(COMPANIES), *g.address())
        d.add(
            _title(
                g, ["Payment Receipt", "Receipt", "Official Receipt", "Acknowledgement of Payment"]
            )
        )
        d.fields(
            g.rng,
            [
                ("Receipt No.", g.number("RCPT-")),
                ("Date", g.day()),
                ("Received from", g.pick(COMPANIES)),
                ("Amount received", g.money()),
                ("Payment method", g.pick(["Bank transfer", "Cheque", "Card", "Cash"])),
                ("For", f"Invoice {g.number('INV-')}"),
            ],
        )
        d.add("Received with thanks. This receipt confirms payment in full.")
    return d.text()


# ------------------------------------------------------------------------------ legal / HR docs
CONTRACT_SECTIONS = (
    (
        "Definitions",
        "In this Agreement capitalised terms have the meanings set out in this clause.",
    ),
    (
        "Term and Termination",
        "This Agreement commences on the Effective Date and continues for an initial term of {n} months. Either Party may terminate this Agreement upon {m} days written notice.",
    ),
    (
        "Services",
        "The Supplier shall provide the Services described in Schedule A with reasonable skill and care.",
    ),
    (
        "Fees and Payment",
        "The Customer shall pay the fees set out in Schedule B within {m} days of receipt of a valid invoice.",
    ),
    (
        "Confidentiality",
        "Each Party shall keep confidential all Confidential Information of the other Party and shall not disclose it to any third party.",
    ),
    (
        "Limitation of Liability",
        "Neither Party shall be liable for any indirect or consequential loss. The total liability of each Party shall not exceed the fees paid in the preceding twelve months.",
    ),
    (
        "Indemnification",
        "The Supplier shall indemnify and hold harmless the Customer against all claims arising from a breach of this Agreement.",
    ),
    (
        "Governing Law",
        "This Agreement shall be governed by and construed in accordance with the laws of {law}.",
    ),
    (
        "Force Majeure",
        "Neither Party shall be in breach of this Agreement if it is prevented from performing by events beyond its reasonable control.",
    ),
    (
        "Assignment",
        "Neither Party may assign this Agreement without the prior written consent of the other Party.",
    ),
    (
        "Entire Agreement",
        "This Agreement constitutes the entire agreement between the Parties and supersedes all prior understandings.",
    ),
)


def _contract(g: _Gen) -> str:
    d = _Doc()
    kind = g.pick(
        [
            "Service Agreement",
            "Master Services Agreement",
            "Supply Agreement",
            "Non-Disclosure Agreement",
            "Consulting Agreement",
            "Lease Agreement",
            "Software Licence Agreement",
            "Framework Contract",
        ]
    )
    d.add(_title(g, [kind], omit=0.08))
    a, b = g.pick(COMPANIES), g.pick(COMPANIES)
    d.add(
        g.pick(
            [
                f'This {kind} (the "Agreement") is entered into as of {g.day()} by and between {a} (the "Supplier") and {b} (the "Customer").',
                f'THIS AGREEMENT is made on {g.day()} BETWEEN {a} (hereinafter "Provider") AND {b} (hereinafter "Client"), each a "Party".',
            ]
        )
    )
    d.add(
        "WHEREAS the Parties wish to set out the terms on which "
        + g.pick(
            [
                "services will be provided",
                "goods will be supplied",
                "confidential information will be shared",
                "the premises will be let",
            ]
        )
        + "; NOW, THEREFORE, the Parties agree as follows:"
    )
    sections = g.rng.sample(CONTRACT_SECTIONS, g.rng.randint(3, 7))
    law = g.pick(
        ["England and Wales", "the State of Delaware", "Germany", "the Netherlands", "India"]
    )
    for number, (heading, body) in enumerate(sections, 1):
        d.add(
            f"{number}. {heading}",
            body.format(n=g.pick([12, 24, 36]), m=g.pick([30, 60, 90]), law=law),
        )
    d.add(
        "IN WITNESS WHEREOF the Parties have executed this Agreement as of the date first written above."
        if g.maybe(0.7)
        else "Signed for and on behalf of the Parties:"
    )
    for party in (a, b):
        d.add(
            f"For {party}",
            "By: ____________________",
            f"Name: {g.person()}",
            f"Title: {g.pick(['Director', 'CEO', 'General Counsel', 'Managing Director'])}",
            "Date: __________",
        )
    return d.text()


POLICY_SECTIONS = (
    (
        "Purpose",
        "This policy sets out the rules for {topic} and the responsibilities of everyone involved.",
    ),
    (
        "Scope",
        "This policy applies to all employees, contractors and temporary staff of the company in all locations.",
    ),
    (
        "Policy Statement",
        "Employees must {rule}. Exceptions require written approval from the policy owner.",
    ),
    (
        "Responsibilities",
        "Managers are responsible for ensuring their teams comply with this policy. The {owner} monitors compliance.",
    ),
    (
        "Approval Limits",
        "Spending above {limit} requires approval by a second authorised signatory.",
    ),
    (
        "Compliance",
        "Violations of this policy may result in disciplinary action up to and including termination of employment.",
    ),
    (
        "Exceptions",
        "Requests for exceptions must be submitted to the policy owner and are reviewed case by case.",
    ),
    ("Review", "This policy is reviewed annually or when regulations change."),
    (
        "Related Documents",
        "See also the Code of Conduct, the Information Security Standard and the Delegation of Authority matrix.",
    ),
)
POLICY_TOPICS = (
    (
        "Travel and Expense Policy",
        "business travel and expense claims",
        "book travel through the approved portal and submit receipts within 30 days",
    ),
    (
        "Information Security Policy",
        "protecting company information",
        "use multi-factor authentication and never share passwords",
    ),
    (
        "Procurement Policy",
        "purchasing goods and services",
        "obtain three quotations for purchases above the threshold and raise a purchase order before ordering",
    ),
    (
        "Code of Conduct",
        "ethical behaviour at work",
        "act with integrity and report conflicts of interest",
    ),
    (
        "Data Retention Policy",
        "how long records are kept",
        "retain financial records for ten years and delete personal data when no longer needed",
    ),
    (
        "Remote Work Policy",
        "working from home",
        "keep company devices secure and be reachable during core hours",
    ),
    (
        "Anti-Bribery Policy",
        "preventing bribery and corruption",
        "not offer or accept gifts above the permitted value",
    ),
)


def _policy(g: _Gen) -> str:
    d = _Doc()
    title, topic, rule = g.pick(POLICY_TOPICS)
    d.add(g.pick(COMPANIES))
    d.add(_title(g, [title, f"{title} (Internal)", f"Corporate {title}"], omit=0.08))
    d.fields(
        g.rng,
        [
            (
                "Policy Owner",
                g.pick(
                    [
                        "Chief Financial Officer",
                        "Head of IT Security",
                        "HR Director",
                        "Compliance Office",
                    ]
                ),
            ),
            ("Effective Date", g.day()),
            ("Version", f"{g.rng.randint(1, 5)}.{g.rng.randint(0, 9)}"),
            ("Next Review", g.day(365)),
            ("Approved by", g.pick(["Executive Board", "Audit Committee", g.person()])),
        ],
    )
    sections = g.rng.sample(POLICY_SECTIONS, g.rng.randint(4, 8))
    for number, (heading, body) in enumerate(sections, 1):
        d.add(
            f"{number}. {heading}",
            body.format(
                topic=topic,
                rule=rule,
                owner=g.pick(["Finance team", "Compliance Office", "IT department"]),
                limit=g.money(1000, 50000),
            ),
        )
    return d.text()


def _resume(g: _Gen) -> str:
    d = _Doc()
    name = g.person()
    role = g.pick(
        [
            "Procurement Specialist",
            "Senior Accountant",
            "Software Engineer",
            "Operations Manager",
            "Data Analyst",
            "Logistics Coordinator",
            "HR Business Partner",
            "Project Manager",
        ]
    )
    d.add(name.upper() if g.maybe(0.5) else name, role)
    d.add(
        f"{g.email(name)} | {g.phone()} | {g.pick(CITIES)}",
        f"linkedin.example.com/in/{name.lower().replace(' ', '-')}" if g.maybe(0.5) else "",
    )
    if g.maybe(0.3):
        d.add(_title(g, ["Curriculum Vitae", "Resume", "CV", "Lebenslauf"], omit=0.0))
    d.add(
        g.pick(["Professional Summary", "Profile", "Summary"]),
        f"{g.pick(['Results-driven', 'Detail-oriented', 'Experienced'])} {role.lower()} with {g.rng.randint(2, 15)} years of experience in "
        f"{g.pick(['supplier management', 'financial reporting', 'cloud systems', 'supply chain operations', 'analytics'])}.",
    )
    jobs = []
    year = 2026
    for _ in range(g.rng.randint(1, 4)):
        start = year - g.rng.randint(1, 5)
        jobs.append(
            f"{g.pick([role, 'Analyst', 'Team Lead', 'Associate', 'Coordinator'])} - {g.pick(COMPANIES)}  {start} - {year if year < 2026 else 'Present'}"
        )
        jobs.append(
            "- "
            + g.pick(
                [
                    "Reduced processing time by 30% by automating invoice matching.",
                    "Managed a portfolio of 120 suppliers across three regions.",
                    "Led a team of 6 analysts and introduced monthly KPI reporting.",
                    "Implemented a new ERP module on time and under budget.",
                    "Negotiated framework contracts saving 1.2M annually.",
                ]
            )
        )
        year = start
    d.add(
        g.pick(["Work Experience", "Professional Experience", "Experience", "Employment History"]),
        *jobs,
    )
    d.add(
        g.pick(["Education", "Academic Background"]),
        f"{g.pick(['B.Sc.', 'M.Sc.', 'MBA', 'B.Com', 'B.Eng.'])} {g.pick(['Business Administration', 'Computer Science', 'Finance', 'Industrial Engineering'])}, "
        f"{g.pick(['University of Northbridge', 'Lakeside Institute of Technology', 'Hanseatic University'])}, {year - g.rng.randint(0, 4)}",
    )
    d.add(
        g.pick(["Skills", "Core Competencies", "Technical Skills"]),
        ", ".join(
            g.rng.sample(
                [
                    "SAP",
                    "Excel",
                    "Python",
                    "SQL",
                    "Negotiation",
                    "IFRS",
                    "Power BI",
                    "Contract management",
                    "Stakeholder management",
                    "Lean Six Sigma",
                    "German (fluent)",
                    "Spanish (basic)",
                ],
                5,
            )
        ),
    )
    if g.maybe(0.4):
        d.add("References available on request.")
    return d.text()


def _bank_statement(g: _Gen) -> str:
    d = _Doc()
    bank = g.pick(BANKS)
    d.add(bank, *g.address())
    d.add(
        _title(
            g,
            [
                "Account Statement",
                "Bank Statement",
                "Statement of Account",
                "Kontoauszug",
                "Monthly Statement",
            ],
        )
    )
    holder = g.pick(COMPANIES) if g.maybe(0.6) else g.person()
    d.fields(
        g.rng,
        [
            ("Account holder", holder),
            ("Account number", f"****{g.rng.randint(1000, 9999)}"),
            (
                "IBAN",
                f"XX{g.rng.randint(10, 99)} {g.rng.randint(1000, 9999)} 0000 {g.rng.randint(1000, 9999)}",
            ),
            ("Statement period", f"{g.day(-30)} - {g.day()}"),
            ("Currency", g.pick(["EUR", "USD", "GBP", "INR"])),
        ],
    )
    d.fields(
        g.rng,
        [
            (
                g.pick(["Opening balance", "Balance brought forward", "Previous balance"]),
                g.amount(100, 90000),
            )
        ],
    )
    rows = []
    for offset in range(g.rng.randint(3, 15)):
        description = g.pick(
            [
                "Card payment " + g.pick(STORES),
                "Direct debit " + g.pick(COMPANIES),
                "Transfer from " + g.pick(COMPANIES),
                "ATM withdrawal",
                "Salary",
                "Interest credit",
                "Standing order rent",
                "Bank charges",
                "Incoming payment INV-" + str(g.rng.randint(1000, 9999)),
            ]
        )
        debit, credit = (g.amount(5, 4000), "") if g.maybe(0.6) else ("", g.amount(5, 9000))
        rows.append([g.day(offset - 30), description, debit, credit, g.amount(100, 90000)])
    d.table(
        [
            "Date",
            "Description",
            g.pick(["Debit", "Withdrawals", "Paid out"]),
            g.pick(["Credit", "Deposits", "Paid in"]),
            "Balance",
        ],
        rows,
    )
    d.fields(
        g.rng,
        [
            (
                g.pick(["Closing balance", "Balance carried forward", "New balance"]),
                g.amount(100, 90000),
            )
        ],
    )
    d.add(
        g.pick(
            [
                "Please check this statement and report any discrepancies within 30 days.",
                "Deposits are protected up to the statutory limit.",
                "This statement was produced electronically.",
            ]
        )
    )
    return d.text()


# ------------------------------------------------------------------------------ other
def _other(g: _Gen) -> str:
    d = _Doc()
    kind = g.pick(
        [
            "memo",
            "minutes",
            "letter",
            "quotation",
            "announcement",
            "itinerary",
            "datasheet",
            "cover_letter",
        ]
    )
    if kind == "memo":
        d.add(_title(g, ["Memorandum", "Internal Memo", "MEMO"], omit=0.1))
        d.fields(
            g.rng,
            [
                ("To", g.pick(["All staff", "Finance team", g.person()])),
                ("From", g.person()),
                ("Date", g.day()),
                (
                    "Subject",
                    g.pick(
                        [
                            "Office move",
                            "Quarter-end close timetable",
                            "New expense tool",
                            "Holiday schedule",
                        ]
                    ),
                ),
            ],
        )
        d.add(
            g.pick(
                [
                    "Please note that the office will be closed on Friday for maintenance.",
                    "The quarter-end close timetable is attached; please submit accruals by day 3.",
                    "From next month, expense claims are submitted in the new tool only.",
                ]
            )
        )
    elif kind == "minutes":
        d.add(
            _title(
                g, ["Minutes of Meeting", "Meeting Minutes", "Steering Committee Minutes"], omit=0.1
            )
        )
        d.fields(
            g.rng,
            [
                ("Date", g.day()),
                ("Attendees", ", ".join(g.person() for _ in range(3))),
                ("Chair", g.person()),
            ],
        )
        d.add("Agenda", "1. Review of action items", "2. Project status", "3. Any other business")
        d.add(
            "Action items",
            f"- {g.person()} to circulate the updated plan by {g.day(7)}",
            f"- {g.person()} to confirm budget",
        )
    elif kind == "letter":
        d.add(g.pick(COMPANIES), *g.address(), g.day())
        d.add(f"Dear {g.pick(['Sir or Madam', g.person(), 'Customer'])},")
        d.add(
            g.pick(
                [
                    "We are pleased to inform you that our new service centre opens next month.",
                    "Thank you for your recent enquiry regarding our product range.",
                    "We write to confirm the change of our registered office address.",
                ]
            )
        )
        d.add(g.pick(["Yours faithfully,", "Kind regards,", "Sincerely,"]), g.person())
    elif kind == "quotation":
        d.add(g.pick(COMPANIES), *g.address())
        d.add(_title(g, ["Quotation", "Quote", "Price Offer", "Estimate", "Angebot"], omit=0.05))
        d.fields(
            g.rng,
            [
                ("Quote No.", g.number("Q-")),
                ("Date", g.day()),
                ("Valid until", g.day(30)),
                ("Prepared for", g.pick(COMPANIES)),
            ],
        )
        d.table(
            ["#", "Item", "Description", "Qty", "Unit", "Unit Price", "Amount"],
            g.line_items(prices=True, count=g.rng.randint(1, 5)),
        )
        d.fields(g.rng, [("Estimated total", g.money())])
        d.add(
            "This quotation is valid for 30 days. Prices exclude delivery. Please contact us to place an order."
        )
    elif kind == "announcement":
        d.add(_title(g, ["Company Announcement", "Newsletter", "Press Release"], omit=0.1))
        d.add(
            g.pick(
                [
                    "We are delighted to welcome our new Chief Operating Officer.",
                    "Our annual sustainability report shows a 12% reduction in emissions.",
                    "Join us for the summer team event on the terrace.",
                ]
            ),
            "For more information contact the communications team.",
        )
    elif kind == "itinerary":
        d.add(_title(g, ["Travel Itinerary", "Booking Confirmation", "Trip Summary"], omit=0.1))
        d.fields(
            g.rng,
            [
                ("Traveller", g.person()),
                ("Booking reference", g.number("BK", 6)),
                ("Flight", f"XY{g.rng.randint(100, 999)}"),
                ("Departure", f"{g.day()} 08:15"),
                ("Hotel", "Harbour View Hotel, 2 nights"),
            ],
        )
    elif kind == "datasheet":
        description, _unit, _ = g.pick(PRODUCTS)
        d.add(
            _title(g, ["Product Data Sheet", "Technical Specification", "Datasheet"], omit=0.1),
            description,
        )
        d.fields(
            g.rng,
            [
                ("Material", g.pick(["Steel", "Brass", "Polypropylene", "Aluminium"])),
                ("Operating temperature", "-20 to 80 C"),
                ("Dimensions", f"{g.rng.randint(10, 500)} x {g.rng.randint(10, 500)} mm"),
                ("Weight", f"{g.rng.randint(1, 900)} g"),
            ],
        )
    else:  # cover letter: resume vocabulary, but a letter
        name = g.person()
        d.add(name, g.email(name), g.day())
        d.add("Dear Hiring Manager,")
        d.add(
            f"I am writing to apply for the position of {g.pick(['Procurement Specialist', 'Accountant', 'Analyst'])} at {g.pick(COMPANIES)}. "
            "My experience and skills make me a strong candidate, as detailed in my enclosed CV."
        )
        d.add("I look forward to hearing from you.", "Sincerely,", name)
    return d.text()


GENERATORS: dict[DocumentType, Callable[[_Gen], str]] = {
    DocumentType.INVOICE: _invoice,
    DocumentType.PURCHASE_ORDER: _purchase_order,
    DocumentType.DELIVERY_NOTE: _delivery_note,
    DocumentType.RECEIPT: _receipt,
    DocumentType.CONTRACT: _contract,
    DocumentType.POLICY: _policy,
    DocumentType.RESUME: _resume,
    DocumentType.BANK_STATEMENT: _bank_statement,
    DocumentType.OTHER: _other,
}


def ocr_noise(text: str, rng: random.Random, rate: float) -> str:
    """Simulate OCR errors: glyph confusions, dropped and merged spaces."""
    out: list[str] = []
    index = 0
    while index < len(text):
        if rng.random() < rate:
            for source, target in OCR_CONFUSIONS:
                if text.startswith(source, index):
                    out.append(target)
                    index += len(source)
                    break
            else:
                if text[index] == " " and rng.random() < 0.5:
                    index += 1  # merged words
                    continue
                out.append(text[index])
                index += 1
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def generate_document(doc_type: DocumentType, rng: random.Random, *, noise: bool = True) -> str:
    text = GENERATORS[doc_type](_Gen(rng))
    if noise and rng.random() < 0.25:
        text = ocr_noise(text, rng, rate=rng.uniform(0.01, 0.05))
    return text


def generate_corpus(*, seed: int, per_class: int) -> list[LabelledText]:
    rng = random.Random(seed)  # noqa: S311  (reproducible synthetic data, not security)
    samples = [
        LabelledText(generate_document(doc_type, rng), doc_type)
        for doc_type in GENERATORS
        for _ in range(per_class)
    ]
    rng.shuffle(samples)
    return samples
