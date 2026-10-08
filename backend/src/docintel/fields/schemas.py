"""Versioned extraction schemas, one per document type (Module 6).

The Pydantic models are what an LLM extractor must return: every scalar field is an
`ExtractedValue` carrying the value *as printed*, the page and the verbatim source text, so each
value can be traced back and verified (Module 7). Table rows carry their own page and source text.
Values stay strings here; typing and normalization happen in code (Module 8) - the model
transcribes, it never computes.

Each field also carries `FieldMeta` / `ColumnMeta` (kept out of the JSON schema by Pydantic):
the value type, whether it is required, and the label synonyms the deterministic layout
extractor uses. Bumping a schema's fields means bumping its version.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from docintel.db.models import DocumentType


class ValueType(StrEnum):
    TEXT = "TEXT"
    IDENTIFIER = "IDENTIFIER"
    ORGANIZATION = "ORGANIZATION"
    PERSON = "PERSON"
    DATE = "DATE"
    MONEY = "MONEY"
    CURRENCY = "CURRENCY"
    PERCENT = "PERCENT"
    QUANTITY = "QUANTITY"
    INTEGER = "INTEGER"
    DAYS = "DAYS"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    BOOLEAN = "BOOLEAN"


@dataclass(frozen=True, slots=True)
class FieldMeta:
    """How a scalar field is typed, validated and found by the layout extractor."""

    type: ValueType
    required: bool = False
    # Printed label synonyms, most specific first (normalized with `normalize_label`).
    labels: tuple[str, ...] = ()
    # Which occurrence wins when a label repeats with different values (totals: the last one).
    occurrence: Literal["first", "last"] = "first"
    # Strength of the letterhead fallback (largest text at the top of page 1) for issuer
    # names; 0 disables it. Lower where the issuer is often not the party asked for.
    letterhead: float = 0.0
    # The value may be printed inside the label itself, e.g. the rate in "VAT (20%)".
    value_in_label: bool = False
    # For a printed range ("01/03/2026 - 31/03/2026"): which end this field is.
    range_part: Literal["start", "end"] | None = None
    # Resolve against the vendor master (Phase 4 normalization).
    vendor: bool = False


@dataclass(frozen=True, slots=True)
class ColumnMeta:
    """A table column: value type and the header texts that identify it."""

    type: ValueType
    headers: tuple[str, ...] = ()
    required: bool = False
    # Counts towards the document's review routing (row numbers and units do not).
    routing: bool = True


@dataclass(frozen=True, slots=True)
class ListMeta:
    """A list of short values printed under a section heading (e.g. resume skills)."""

    type: ValueType
    sections: tuple[str, ...] = ()


class ExtractedValue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: str = Field(max_length=500, description="The value exactly as printed in the document")
    page: int = Field(ge=1, description="1-based page number where the value is printed")
    source_text: str = Field(
        max_length=500,
        description="Verbatim text from that page containing the value (label and value)",
    )


class TableRowBase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page: int = Field(ge=1, description="Page the row is printed on")
    source_text: str = Field(max_length=1000, description="The whole row exactly as printed")


Value = ExtractedValue | None
Cell = str | None


# ------------------------------------------------------------------------------ shared labels
_TAX_LABELS = (
    "sales tax",
    "vat",
    "gst",
    "igst",
    "mwst",
    "ust",
    "tax",
    "value added tax",
)
_SUBTOTAL_LABELS = (
    "subtotal",
    "sub total",
    "net total",
    "total net",
    "net amount",
    "amount before tax",
    "total before tax",
    "zwischensumme",
    "nettobetrag",
)
_TAX_ID_LABELS = (
    "tax id",
    "vat no",
    "vat id",
    "vat reg no",
    "vat registration no",
    "tax no",
    "tin",
    "ein",
    "gstin",
    "gst no",
    "ust idnr",
)
_CURRENCY_LABELS = ("currency", "währung", "waehrung")
_TERMS_LABELS = ("payment terms", "terms of payment", "terms", "zahlungsbedingungen")
_PO_REF_LABELS = (
    "po reference",
    "po ref",
    "purchase order no",
    "purchase order",
    "po no",
    "your order no",
    "your order",
    "customer po",
    "order reference",
    "order no",
)

LINE_NUMBER = ColumnMeta(
    ValueType.INTEGER, ("#", "no", "line", "pos", "position", "ln"), routing=False
)
SKU = ColumnMeta(
    ValueType.IDENTIFIER,
    (
        "sku",
        "item code",
        "item no",
        "part no",
        "article no",
        "article",
        "product code",
        "code",
        "item",
    ),
)
DESCRIPTION = ColumnMeta(
    ValueType.TEXT,
    ("description", "item description", "product", "details", "particulars", "service"),
    required=True,
)
UNIT = ColumnMeta(ValueType.TEXT, ("unit", "uom", "unit of measure", "einheit"), routing=False)
UNIT_PRICE = ColumnMeta(
    ValueType.MONEY,
    ("unit price", "price", "rate", "unit cost", "price per unit", "einzelpreis"),
)
LINE_AMOUNT = ColumnMeta(
    ValueType.MONEY,
    ("amount", "line total", "total", "net amount", "extended price", "ext price", "betrag"),
)


class PricedLine(TableRowBase):
    line_number: Annotated[Cell, LINE_NUMBER] = Field(
        default=None, description="Line or position number"
    )
    sku: Annotated[Cell, SKU] = Field(default=None, description="Item code / SKU / part number")
    description: Annotated[Cell, DESCRIPTION] = Field(default=None, description="Item description")
    quantity: Annotated[
        Cell, ColumnMeta(ValueType.QUANTITY, ("qty", "quantity", "qty ordered", "units", "menge"))
    ] = Field(default=None, description="Quantity")
    unit: Annotated[Cell, UNIT] = Field(default=None, description="Unit of measure")
    unit_price: Annotated[Cell, UNIT_PRICE] = Field(
        default=None, description="Price per unit as printed"
    )
    amount: Annotated[Cell, LINE_AMOUNT] = Field(default=None, description="Line total as printed")


class DeliveredLine(TableRowBase):
    line_number: Annotated[Cell, LINE_NUMBER] = Field(
        default=None, description="Line or position number"
    )
    sku: Annotated[Cell, SKU] = Field(default=None, description="Item code / SKU / part number")
    description: Annotated[Cell, DESCRIPTION] = Field(default=None, description="Item description")
    quantity: Annotated[
        Cell,
        ColumnMeta(
            ValueType.QUANTITY,
            ("qty delivered", "qty shipped", "quantity delivered", "qty", "quantity", "menge"),
        ),
    ] = Field(default=None, description="Quantity delivered")
    unit: Annotated[Cell, UNIT] = Field(default=None, description="Unit of measure")


class ReceiptLine(TableRowBase):
    description: Annotated[Cell, DESCRIPTION] = Field(default=None, description="Item description")
    quantity: Annotated[Cell, ColumnMeta(ValueType.QUANTITY, ("qty", "quantity"))] = Field(
        default=None, description="Quantity"
    )
    unit_price: Annotated[Cell, UNIT_PRICE] = Field(
        default=None, description="Price per unit as printed"
    )
    amount: Annotated[Cell, LINE_AMOUNT] = Field(default=None, description="Line total as printed")


class StatementTransaction(TableRowBase):
    date: Annotated[
        Cell,
        ColumnMeta(
            ValueType.DATE,
            ("date", "transaction date", "value date", "posting date", "booking date"),
            required=True,
        ),
    ] = Field(default=None, description="Transaction date as printed")
    description: Annotated[
        Cell,
        ColumnMeta(
            ValueType.TEXT,
            ("description", "details", "transaction", "particulars", "narrative"),
            required=True,
        ),
    ] = Field(default=None, description="Transaction description")
    debit: Annotated[
        Cell,
        ColumnMeta(
            ValueType.MONEY,
            ("debit", "debits", "withdrawal", "withdrawals", "paid out", "money out"),
        ),
    ] = Field(default=None, description="Amount taken out of the account")
    credit: Annotated[
        Cell,
        ColumnMeta(
            ValueType.MONEY,
            ("credit", "credits", "deposit", "deposits", "paid in", "money in"),
        ),
    ] = Field(default=None, description="Amount paid into the account")
    balance: Annotated[Cell, ColumnMeta(ValueType.MONEY, ("balance", "running balance"))] = Field(
        default=None, description="Balance after the row"
    )


# ------------------------------------------------------------------------------ schemas
class ExtractionSchema(BaseModel):
    """Base class: subclasses declare fields; `SCHEMA_NAME`/`SCHEMA_VERSION` identify them."""

    model_config = ConfigDict(extra="ignore")

    SCHEMA_NAME: ClassVar[str]
    SCHEMA_VERSION: ClassVar[int]
    DOCUMENT_TYPE: ClassVar[DocumentType]
    # The table (line items, transactions) must have rows; "none" needs a reviewer's word.
    ROWS_REQUIRED: ClassVar[bool] = False


_VENDOR_LABELS = ("vendor", "supplier", "seller", "sold by", "issued by", "from", "remit to")


class InvoiceV1(ExtractionSchema):
    SCHEMA_NAME: ClassVar[str] = "invoice"
    SCHEMA_VERSION: ClassVar[int] = 1
    DOCUMENT_TYPE: ClassVar[DocumentType] = DocumentType.INVOICE
    ROWS_REQUIRED: ClassVar[bool] = True

    vendor_name: Annotated[
        Value,
        FieldMeta(
            ValueType.ORGANIZATION,
            required=True,
            labels=_VENDOR_LABELS,
            letterhead=0.9,
            vendor=True,
        ),
    ] = Field(default=None, description="Name of the company that issued the invoice (the seller)")
    vendor_tax_id: Annotated[Value, FieldMeta(ValueType.IDENTIFIER, labels=_TAX_ID_LABELS)] = Field(
        default=None, description="Seller's tax / VAT registration number"
    )
    invoice_number: Annotated[
        Value,
        FieldMeta(
            ValueType.IDENTIFIER,
            required=True,
            labels=(
                "invoice no",
                "invoice id",
                "inv no",
                "bill no",
                "rechnungsnummer",
                "rechnung no",
            ),
        ),
    ] = Field(default=None, description="Invoice number")
    invoice_date: Annotated[
        Value,
        FieldMeta(
            ValueType.DATE,
            required=True,
            labels=(
                "invoice date",
                "date of issue",
                "issue date",
                "billing date",
                "rechnungsdatum",
                "date",
            ),
        ),
    ] = Field(default=None, description="Date the invoice was issued, as printed")
    due_date: Annotated[
        Value,
        FieldMeta(
            ValueType.DATE,
            labels=("due date", "payment due", "due by", "pay by", "fällig am"),
        ),
    ] = Field(default=None, description="Payment due date, as printed")
    purchase_order_number: Annotated[
        Value, FieldMeta(ValueType.IDENTIFIER, labels=_PO_REF_LABELS)
    ] = Field(
        default=None, description="The buyer's purchase order number referenced by the invoice"
    )
    buyer_name: Annotated[
        Value,
        FieldMeta(
            ValueType.ORGANIZATION,
            labels=("bill to", "billed to", "invoice to", "sold to", "customer", "buyer", "client"),
        ),
    ] = Field(default=None, description="Name of the customer being billed")
    currency: Annotated[Value, FieldMeta(ValueType.CURRENCY, labels=_CURRENCY_LABELS)] = Field(
        default=None, description="Currency code or symbol as printed"
    )
    payment_terms_days: Annotated[Value, FieldMeta(ValueType.DAYS, labels=_TERMS_LABELS)] = Field(
        default=None, description="Payment terms as printed, e.g. 'Net 30 days'"
    )
    subtotal: Annotated[
        Value, FieldMeta(ValueType.MONEY, labels=_SUBTOTAL_LABELS, occurrence="last")
    ] = Field(default=None, description="Total before tax, as printed")
    tax_amount: Annotated[
        Value, FieldMeta(ValueType.MONEY, labels=_TAX_LABELS, occurrence="last")
    ] = Field(default=None, description="Tax amount, as printed")
    tax_rate: Annotated[
        Value,
        FieldMeta(
            ValueType.PERCENT,
            labels=("tax rate", "vat rate", *_TAX_LABELS),
            occurrence="last",
            value_in_label=True,
        ),
    ] = Field(default=None, description="Tax rate as printed, e.g. '20%'")
    total: Annotated[
        Value,
        FieldMeta(
            ValueType.MONEY,
            required=True,
            labels=(
                "total due",
                "amount due",
                "balance due",
                "invoice total",
                "grand total",
                "total amount",
                "total payable",
                "amount payable",
                "total",
                "gesamtbetrag",
                "rechnungsbetrag",
            ),
            occurrence="last",
        ),
    ] = Field(default=None, description="Total amount to pay including tax, as printed")
    line_items: list[PricedLine] = Field(
        default_factory=list, description="Every line item row of the invoice"
    )


class PurchaseOrderV1(ExtractionSchema):
    SCHEMA_NAME: ClassVar[str] = "purchase_order"
    SCHEMA_VERSION: ClassVar[int] = 1
    DOCUMENT_TYPE: ClassVar[DocumentType] = DocumentType.PURCHASE_ORDER
    ROWS_REQUIRED: ClassVar[bool] = True

    po_number: Annotated[
        Value,
        FieldMeta(
            ValueType.IDENTIFIER,
            required=True,
            labels=(
                "po no",
                "purchase order no",
                "order no",
                "po id",
                "bestellnummer",
                "bestellung no",
            ),
        ),
    ] = Field(default=None, description="Purchase order number")
    po_date: Annotated[
        Value,
        FieldMeta(
            ValueType.DATE,
            required=True,
            labels=("order date", "po date", "date of order", "bestelldatum", "date"),
        ),
    ] = Field(default=None, description="Date of the order, as printed")
    # On many purchase orders the letterhead is the buyer's, so the letterhead is a weak hint.
    vendor_name: Annotated[
        Value,
        FieldMeta(
            ValueType.ORGANIZATION,
            required=True,
            labels=("vendor", "supplier", "seller", "to", "order to"),
            letterhead=0.75,
            vendor=True,
        ),
    ] = Field(default=None, description="Name of the supplier the goods are ordered from")
    vendor_tax_id: Annotated[Value, FieldMeta(ValueType.IDENTIFIER, labels=_TAX_ID_LABELS)] = Field(
        default=None, description="Supplier's tax / VAT registration number"
    )
    buyer_name: Annotated[
        Value,
        FieldMeta(
            ValueType.ORGANIZATION,
            labels=("bill to", "buyer", "invoice to", "ordered by", "customer", "purchaser"),
        ),
    ] = Field(default=None, description="Name of the ordering company")
    delivery_date: Annotated[
        Value,
        FieldMeta(
            ValueType.DATE,
            labels=("delivery date", "deliver by", "required by", "requested delivery date"),
        ),
    ] = Field(default=None, description="Requested delivery date, as printed")
    currency: Annotated[Value, FieldMeta(ValueType.CURRENCY, labels=_CURRENCY_LABELS)] = Field(
        default=None, description="Currency code or symbol as printed"
    )
    payment_terms_days: Annotated[Value, FieldMeta(ValueType.DAYS, labels=_TERMS_LABELS)] = Field(
        default=None, description="Payment terms as printed, e.g. 'Net 30 days'"
    )
    subtotal: Annotated[
        Value, FieldMeta(ValueType.MONEY, labels=_SUBTOTAL_LABELS, occurrence="last")
    ] = Field(default=None, description="Total before tax, as printed")
    tax_amount: Annotated[
        Value, FieldMeta(ValueType.MONEY, labels=_TAX_LABELS, occurrence="last")
    ] = Field(default=None, description="Tax amount, as printed")
    tax_rate: Annotated[
        Value,
        FieldMeta(
            ValueType.PERCENT,
            labels=("tax rate", "vat rate", *_TAX_LABELS),
            occurrence="last",
            value_in_label=True,
        ),
    ] = Field(default=None, description="Tax rate as printed")
    total: Annotated[
        Value,
        FieldMeta(
            ValueType.MONEY,
            required=True,
            labels=(
                "order total",
                "po total",
                "total amount",
                "grand total",
                "total value",
                "total",
                "gesamtbetrag",
            ),
            occurrence="last",
        ),
    ] = Field(default=None, description="Order total including tax, as printed")
    line_items: list[PricedLine] = Field(
        default_factory=list, description="Every ordered line item"
    )


class DeliveryNoteV1(ExtractionSchema):
    SCHEMA_NAME: ClassVar[str] = "delivery_note"
    SCHEMA_VERSION: ClassVar[int] = 1
    DOCUMENT_TYPE: ClassVar[DocumentType] = DocumentType.DELIVERY_NOTE
    ROWS_REQUIRED: ClassVar[bool] = True

    delivery_note_number: Annotated[
        Value,
        FieldMeta(
            ValueType.IDENTIFIER,
            required=True,
            labels=(
                "delivery note no",
                "dn no",
                "delivery no",
                "packing slip no",
                "dispatch note no",
                "shipment no",
                "lieferschein no",
                "lieferscheinnummer",
            ),
        ),
    ] = Field(default=None, description="Delivery note / packing slip number")
    delivery_date: Annotated[
        Value,
        FieldMeta(
            ValueType.DATE,
            required=True,
            labels=("delivery date", "ship date", "dispatch date", "shipped on", "date"),
        ),
    ] = Field(default=None, description="Delivery date, as printed")
    vendor_name: Annotated[
        Value,
        FieldMeta(
            ValueType.ORGANIZATION,
            required=True,
            labels=("supplier", "shipper", "vendor", "from", "sender"),
            letterhead=0.9,
            vendor=True,
        ),
    ] = Field(default=None, description="Name of the supplier that shipped the goods")
    purchase_order_number: Annotated[
        Value, FieldMeta(ValueType.IDENTIFIER, labels=_PO_REF_LABELS)
    ] = Field(default=None, description="Purchase order number the delivery refers to")
    recipient_name: Annotated[
        Value,
        FieldMeta(
            ValueType.ORGANIZATION,
            labels=("ship to", "deliver to", "consignee", "recipient", "delivery address"),
        ),
    ] = Field(default=None, description="Name of the receiving company")
    line_items: list[DeliveredLine] = Field(
        default_factory=list, description="Every delivered line item"
    )


class ReceiptV1(ExtractionSchema):
    SCHEMA_NAME: ClassVar[str] = "receipt"
    SCHEMA_VERSION: ClassVar[int] = 1
    DOCUMENT_TYPE: ClassVar[DocumentType] = DocumentType.RECEIPT

    merchant_name: Annotated[
        Value,
        FieldMeta(
            ValueType.ORGANIZATION,
            required=True,
            labels=("merchant", "store", "seller", "received from"),
            letterhead=0.9,
            vendor=True,
        ),
    ] = Field(default=None, description="Name of the merchant that issued the receipt")
    receipt_number: Annotated[
        Value,
        FieldMeta(
            ValueType.IDENTIFIER,
            labels=("receipt no", "transaction no", "receipt id", "ref no", "bill no", "bon no"),
        ),
    ] = Field(default=None, description="Receipt or transaction number")
    transaction_date: Annotated[
        Value,
        FieldMeta(
            ValueType.DATE,
            required=True,
            labels=("date", "transaction date", "receipt date", "payment date", "datum"),
        ),
    ] = Field(default=None, description="Date of the purchase or payment, as printed")
    payment_method: Annotated[
        Value, FieldMeta(ValueType.TEXT, labels=("payment method", "paid by", "tender", "payment"))
    ] = Field(default=None, description="How it was paid, e.g. card or cash")
    currency: Annotated[Value, FieldMeta(ValueType.CURRENCY, labels=_CURRENCY_LABELS)] = Field(
        default=None, description="Currency code or symbol as printed"
    )
    subtotal: Annotated[
        Value, FieldMeta(ValueType.MONEY, labels=_SUBTOTAL_LABELS, occurrence="last")
    ] = Field(default=None, description="Total before tax, as printed")
    tax_amount: Annotated[
        Value, FieldMeta(ValueType.MONEY, labels=_TAX_LABELS, occurrence="last")
    ] = Field(default=None, description="Tax amount, as printed")
    total: Annotated[
        Value,
        FieldMeta(
            ValueType.MONEY,
            required=True,
            labels=("total paid", "amount paid", "grand total", "total", "summe"),
            occurrence="last",
        ),
    ] = Field(default=None, description="Total paid, as printed")
    line_items: list[ReceiptLine] = Field(default_factory=list, description="Purchased items")


class ContractV1(ExtractionSchema):
    SCHEMA_NAME: ClassVar[str] = "contract"
    SCHEMA_VERSION: ClassVar[int] = 1
    DOCUMENT_TYPE: ClassVar[DocumentType] = DocumentType.CONTRACT

    title: Annotated[Value, FieldMeta(ValueType.TEXT, letterhead=0.8)] = Field(
        default=None, description="Title of the agreement"
    )
    contract_number: Annotated[
        Value,
        FieldMeta(
            ValueType.IDENTIFIER,
            labels=("contract no", "agreement no", "contract id", "contract reference"),
        ),
    ] = Field(default=None, description="Contract or agreement number")
    party_a: Annotated[
        Value,
        FieldMeta(
            ValueType.ORGANIZATION,
            required=True,
            labels=("customer", "client", "buyer", "party a", "first party", "company"),
        ),
    ] = Field(default=None, description="First party (usually the customer / buyer)")
    party_b: Annotated[
        Value,
        FieldMeta(
            ValueType.ORGANIZATION,
            required=True,
            labels=(
                "supplier",
                "service provider",
                "provider",
                "contractor",
                "vendor",
                "party b",
                "second party",
            ),
            vendor=True,
        ),
    ] = Field(default=None, description="Second party (usually the supplier / service provider)")
    effective_date: Annotated[
        Value,
        FieldMeta(
            ValueType.DATE,
            required=True,
            labels=("effective date", "commencement date", "start date", "effective from"),
        ),
    ] = Field(default=None, description="Date the agreement takes effect, as printed")
    expiration_date: Annotated[
        Value,
        FieldMeta(
            ValueType.DATE,
            labels=("expiration date", "expiry date", "end date", "valid until", "term ends"),
        ),
    ] = Field(default=None, description="Date the agreement ends, as printed")
    contract_value: Annotated[
        Value,
        FieldMeta(
            ValueType.MONEY,
            labels=("contract value", "total contract value", "contract price", "fees"),
        ),
    ] = Field(default=None, description="Total value or fee of the agreement, as printed")
    currency: Annotated[Value, FieldMeta(ValueType.CURRENCY, labels=_CURRENCY_LABELS)] = Field(
        default=None, description="Currency code or symbol as printed"
    )
    payment_terms_days: Annotated[Value, FieldMeta(ValueType.DAYS, labels=_TERMS_LABELS)] = Field(
        default=None, description="Payment terms as printed"
    )
    termination_notice_days: Annotated[
        Value,
        FieldMeta(
            ValueType.DAYS, labels=("notice period", "termination notice", "notice of termination")
        ),
    ] = Field(default=None, description="Notice period for termination as printed, e.g. '30 days'")
    governing_law: Annotated[
        Value, FieldMeta(ValueType.TEXT, labels=("governing law", "jurisdiction", "applicable law"))
    ] = Field(default=None, description="Governing law / jurisdiction")
    auto_renewal: Annotated[
        Value, FieldMeta(ValueType.BOOLEAN, labels=("auto renewal", "automatic renewal", "renewal"))
    ] = Field(
        default=None,
        description="Whether the agreement renews automatically: quote the renewal clause",
    )


class PolicyV1(ExtractionSchema):
    SCHEMA_NAME: ClassVar[str] = "policy"
    SCHEMA_VERSION: ClassVar[int] = 1
    DOCUMENT_TYPE: ClassVar[DocumentType] = DocumentType.POLICY

    title: Annotated[
        Value, FieldMeta(ValueType.TEXT, required=True, labels=("policy title",), letterhead=0.85)
    ] = Field(default=None, description="Title of the policy")
    policy_number: Annotated[
        Value,
        FieldMeta(
            ValueType.IDENTIFIER,
            labels=("policy no", "policy id", "document no", "document id", "reference"),
        ),
    ] = Field(default=None, description="Policy or document number")
    version: Annotated[
        Value, FieldMeta(ValueType.IDENTIFIER, labels=("version", "revision", "rev"))
    ] = Field(default=None, description="Version")
    effective_date: Annotated[
        Value,
        FieldMeta(
            ValueType.DATE,
            required=True,
            labels=("effective date", "effective from", "valid from", "in force from"),
        ),
    ] = Field(default=None, description="Date the policy takes effect, as printed")
    next_review_date: Annotated[
        Value, FieldMeta(ValueType.DATE, labels=("next review", "review date", "review by"))
    ] = Field(default=None, description="Next review date, as printed")
    owner: Annotated[
        Value,
        FieldMeta(ValueType.TEXT, labels=("policy owner", "owner", "responsible", "approved by")),
    ] = Field(default=None, description="Owner of the policy (department or role)")
    applies_to: Annotated[
        Value, FieldMeta(ValueType.TEXT, labels=("applies to", "scope", "applicability"))
    ] = Field(default=None, description="Who the policy applies to")


class ResumeV1(ExtractionSchema):
    SCHEMA_NAME: ClassVar[str] = "resume"
    SCHEMA_VERSION: ClassVar[int] = 1
    DOCUMENT_TYPE: ClassVar[DocumentType] = DocumentType.RESUME

    candidate_name: Annotated[
        Value, FieldMeta(ValueType.PERSON, required=True, labels=("name",), letterhead=0.85)
    ] = Field(default=None, description="Full name of the candidate")
    email: Annotated[Value, FieldMeta(ValueType.EMAIL, labels=("email", "e mail"))] = Field(
        default=None, description="E-mail address"
    )
    phone: Annotated[
        Value, FieldMeta(ValueType.PHONE, labels=("phone", "mobile", "tel", "telephone"))
    ] = Field(default=None, description="Phone number")
    location: Annotated[
        Value, FieldMeta(ValueType.TEXT, labels=("location", "address", "based in"))
    ] = Field(default=None, description="City / country")
    current_title: Annotated[
        Value, FieldMeta(ValueType.TEXT, labels=("current role", "current position", "title"))
    ] = Field(default=None, description="Current or most recent job title")
    years_of_experience: Annotated[
        Value, FieldMeta(ValueType.QUANTITY, labels=("years of experience", "experience"))
    ] = Field(default=None, description="Total years of professional experience as stated")
    skills: Annotated[
        list[ExtractedValue],
        ListMeta(ValueType.TEXT, ("skills", "technical skills", "key skills", "core skills")),
        Field(default_factory=list, description="Skills listed by the candidate, one per item"),
    ]


class BankStatementV1(ExtractionSchema):
    SCHEMA_NAME: ClassVar[str] = "bank_statement"
    SCHEMA_VERSION: ClassVar[int] = 1
    DOCUMENT_TYPE: ClassVar[DocumentType] = DocumentType.BANK_STATEMENT
    ROWS_REQUIRED: ClassVar[bool] = True

    bank_name: Annotated[Value, FieldMeta(ValueType.ORGANIZATION, letterhead=0.9)] = Field(
        default=None, description="Name of the bank"
    )
    account_holder: Annotated[
        Value,
        FieldMeta(
            ValueType.TEXT,
            required=True,
            labels=("account holder", "account name", "customer name", "name"),
        ),
    ] = Field(default=None, description="Name of the account holder")
    account_number: Annotated[
        Value,
        FieldMeta(
            ValueType.IDENTIFIER,
            required=True,
            labels=("account no", "account", "iban", "acct no"),
        ),
    ] = Field(default=None, description="Account number or IBAN as printed")
    statement_period_start: Annotated[
        Value,
        FieldMeta(
            ValueType.DATE,
            required=True,
            labels=("statement period", "period", "from", "period from"),
            range_part="start",
        ),
    ] = Field(default=None, description="First day covered by the statement, as printed")
    statement_period_end: Annotated[
        Value,
        FieldMeta(
            ValueType.DATE,
            required=True,
            labels=("statement period", "period", "to", "period to", "statement date"),
            range_part="end",
        ),
    ] = Field(default=None, description="Last day covered by the statement, as printed")
    currency: Annotated[Value, FieldMeta(ValueType.CURRENCY, labels=_CURRENCY_LABELS)] = Field(
        default=None, description="Currency code or symbol as printed"
    )
    opening_balance: Annotated[
        Value,
        FieldMeta(
            ValueType.MONEY,
            required=True,
            labels=(
                "opening balance",
                "balance brought forward",
                "previous balance",
                "beginning balance",
            ),
        ),
    ] = Field(default=None, description="Balance at the start of the period, as printed")
    closing_balance: Annotated[
        Value,
        FieldMeta(
            ValueType.MONEY,
            required=True,
            labels=("closing balance", "balance carried forward", "ending balance", "new balance"),
            occurrence="last",
        ),
    ] = Field(default=None, description="Balance at the end of the period, as printed")
    total_credits: Annotated[
        Value,
        FieldMeta(
            ValueType.MONEY,
            labels=("total credits", "total deposits", "total paid in", "money in"),
            occurrence="last",
        ),
    ] = Field(default=None, description="Sum of credits in the period, as printed")
    total_debits: Annotated[
        Value,
        FieldMeta(
            ValueType.MONEY,
            labels=("total debits", "total withdrawals", "total paid out", "money out"),
            occurrence="last",
        ),
    ] = Field(default=None, description="Sum of debits in the period, as printed")
    transactions: list[StatementTransaction] = Field(
        default_factory=list, description="Every transaction row"
    )


SCHEMAS: dict[DocumentType, type[ExtractionSchema]] = {
    schema.DOCUMENT_TYPE: schema
    for schema in (
        InvoiceV1,
        PurchaseOrderV1,
        DeliveryNoteV1,
        ReceiptV1,
        ContractV1,
        PolicyV1,
        ResumeV1,
        BankStatementV1,
    )
}


# ------------------------------------------------------------------------------ introspection
@dataclass(frozen=True, slots=True)
class ScalarField:
    name: str
    meta: FieldMeta
    description: str


@dataclass(frozen=True, slots=True)
class ColumnField:
    name: str
    meta: ColumnMeta
    description: str


@dataclass(frozen=True, slots=True)
class TableField:
    name: str
    row_model: type[TableRowBase]
    columns: tuple[ColumnField, ...]


@dataclass(frozen=True, slots=True)
class ListField:
    name: str
    meta: ListMeta
    description: str


@dataclass(frozen=True, slots=True)
class SchemaInfo:
    model: type[ExtractionSchema]
    scalars: tuple[ScalarField, ...]
    table: TableField | None
    lists: tuple[ListField, ...]

    @property
    def name(self) -> str:
        return self.model.SCHEMA_NAME

    @property
    def version(self) -> int:
        return self.model.SCHEMA_VERSION

    @property
    def document_type(self) -> DocumentType:
        return self.model.DOCUMENT_TYPE

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.scalars if field.meta.required)

    @property
    def rows_required(self) -> bool:
        return self.table is not None and self.model.ROWS_REQUIRED

    def scalar(self, name: str) -> ScalarField | None:
        return next((field for field in self.scalars if field.name == name), None)

    def column(self, name: str) -> ColumnField | None:
        if self.table is None:
            return None
        return next((column for column in self.table.columns if column.name == name), None)


def _meta[M](metadata: list[Any], kind: type[M]) -> M | None:
    return next((item for item in metadata if isinstance(item, kind)), None)


def describe(model: type[ExtractionSchema]) -> SchemaInfo:
    scalars: list[ScalarField] = []
    lists: list[ListField] = []
    table: TableField | None = None
    for name, info in model.model_fields.items():
        description = info.description or name
        if (meta := _meta(info.metadata, FieldMeta)) is not None:
            scalars.append(ScalarField(name, meta, description))
        elif (list_meta := _meta(info.metadata, ListMeta)) is not None:
            lists.append(ListField(name, list_meta, description))
        else:
            row_model = _row_model(info.annotation)
            if row_model is None:
                msg = f"{model.__name__}.{name} has no extraction metadata"
                raise TypeError(msg)
            columns = tuple(
                ColumnField(column, column_meta, column_info.description or column)
                for column, column_info in row_model.model_fields.items()
                if (column_meta := _meta(column_info.metadata, ColumnMeta)) is not None
            )
            table = TableField(name, row_model, columns)
    return SchemaInfo(model, tuple(scalars), table, tuple(lists))


def _row_model(annotation: Any) -> type[TableRowBase] | None:
    args: tuple[Any, ...] = getattr(annotation, "__args__", ())
    if len(args) == 1 and isinstance(args[0], type) and issubclass(args[0], TableRowBase):
        return args[0]
    return None


SCHEMA_INFO: dict[DocumentType, SchemaInfo] = {
    doc_type: describe(model) for doc_type, model in SCHEMAS.items()
}


def schema_for(document_type: DocumentType | None) -> SchemaInfo | None:
    """The extraction schema for a type; None for OTHER (nothing structured to extract)."""
    if document_type is None:
        return None
    return SCHEMA_INFO.get(document_type)


def schema_by_name(name: str) -> SchemaInfo | None:
    return next((info for info in SCHEMA_INFO.values() if info.name == name), None)
