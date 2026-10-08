"""PDF rendering of synthetic documents (reportlab platypus; deterministic output)."""

from __future__ import annotations

import io
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from docintel.synthetic.catalog import (
    BUYER_ADDRESS,
    BUYER_CONTACT,
    LOCALES,
    VENDORS,
    Locale,
    Vendor,
)
from docintel.synthetic.model import DocumentTruth, SyntheticDocType

TITLES = {
    SyntheticDocType.PURCHASE_ORDER: "PURCHASE ORDER",
    SyntheticDocType.INVOICE: "INVOICE",
    SyntheticDocType.DELIVERY_NOTE: "DELIVERY NOTE",
}
NUMBER_LABELS = {
    SyntheticDocType.PURCHASE_ORDER: "PO Number",
    SyntheticDocType.INVOICE: "Invoice No.",
    SyntheticDocType.DELIVERY_NOTE: "Delivery Note No.",
}
DATE_LABELS = {
    SyntheticDocType.PURCHASE_ORDER: "Order Date",
    SyntheticDocType.INVOICE: "Invoice Date",
    SyntheticDocType.DELIVERY_NOTE: "Delivery Date",
}


def format_amount(value: Decimal, locale: Locale) -> str:
    whole, _, fraction = f"{value:,.2f}".partition(".")
    whole = whole.replace(",", locale.thousands_separator)
    return f"{whole}{locale.decimal_separator}{fraction}"


def format_money(value: Decimal, locale: Locale) -> str:
    amount = format_amount(value, locale)
    return f"{amount} {locale.symbol}" if locale.symbol_after else f"{locale.symbol} {amount}"


def format_quantity(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _vendor(code: str) -> Vendor:
    return next(vendor for vendor in VENDORS if vendor.code == code)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontSize=20, alignment=TA_RIGHT),
        "vendor": ParagraphStyle("vendor", parent=base["Heading2"], fontSize=13, spaceAfter=2),
        "body": ParagraphStyle("body", parent=base["Normal"], fontSize=9, leading=11),
        "small": ParagraphStyle("small", parent=base["Normal"], fontSize=7.5, leading=9),
        "heading": ParagraphStyle("heading", parent=base["Heading4"], fontSize=9, spaceAfter=1),
    }


def _meta_rows(truth: DocumentTruth) -> list[tuple[str, str]]:
    date_format = truth.rendering.date_format
    rows = [
        (NUMBER_LABELS[truth.document_type], truth.number),
        (DATE_LABELS[truth.document_type], truth.issue_date.strftime(date_format)),
    ]
    if truth.document_type != SyntheticDocType.PURCHASE_ORDER and truth.purchase_order_number:
        rows.append(("PO Reference", truth.purchase_order_number))
    if truth.due_date is not None:
        rows.append(("Due Date", truth.due_date.strftime(date_format)))
    if truth.payment_terms_days is not None:
        rows.append(("Payment Terms", f"Net {truth.payment_terms_days} days"))
    rows.append(("Currency", truth.currency))
    return rows


def _line_table(truth: DocumentTruth, locale: Locale, template: str) -> Table:
    if truth.has_prices:
        header = ["#", "Item", "Description", "Qty", "Unit", "Unit Price", "Amount"]
        rows = [
            [
                str(item.line_number),
                item.sku,
                item.description,
                format_quantity(item.quantity),
                item.unit,
                format_amount(item.unit_price, locale),
                format_amount(item.line_total, locale),
            ]
            for item in truth.line_items
        ]
        widths = [8 * mm, 24 * mm, 70 * mm, 14 * mm, 12 * mm, 24 * mm, 26 * mm]
    else:
        header = ["#", "Item", "Description", "Qty Delivered", "Unit"]
        rows = [
            [
                str(item.line_number),
                item.sku,
                item.description,
                format_quantity(item.quantity),
                item.unit,
            ]
            for item in truth.line_items
        ]
        widths = [8 * mm, 28 * mm, 98 * mm, 26 * mm, 18 * mm]
    table = Table([header, *rows], colWidths=widths, repeatRows=1)
    style: list[Any] = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]
    if template == "classic":
        style += [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF7")),
        ]
    elif template == "modern":
        style += [
            ("LINEBELOW", (0, 0), (-1, 0), 1.0, colors.HexColor("#1E3A8A")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F4F6")]),
        ]
    else:  # compact
        style += [("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey)]
    table.setStyle(TableStyle(style))
    return table


def _totals(truth: DocumentTruth, locale: Locale) -> Table:
    rate = truth.tax_rate or Decimal(0)
    rows = [
        ["Subtotal", format_money(truth.printed_subtotal or Decimal(0), locale)],
        [
            f"{truth.tax_label} ({format_quantity(rate * 100)}%)",
            format_money(truth.printed_tax or Decimal(0), locale),
        ],
        [
            "Total Due" if truth.document_type == SyntheticDocType.INVOICE else "Order Total",
            format_money(truth.printed_total or Decimal(0), locale),
        ],
    ]
    table = Table(rows, colWidths=[45 * mm, 38 * mm], hAlign="RIGHT")
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
                ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 10),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
            ]
        )
    )
    return table


def _address_block(
    title: str, lines: Sequence[str], styles: dict[str, ParagraphStyle]
) -> list[Flowable]:
    return [
        Paragraph(title, styles["heading"]),
        *[Paragraph(line, styles["body"]) for line in lines],
    ]


def render_pdf(truth: DocumentTruth) -> bytes:
    vendor = _vendor(truth.vendor_code)
    locale = LOCALES[vendor.country]
    template = truth.rendering.template
    styles = _styles()
    story: list[Flowable] = []

    vendor_block = [
        Paragraph(truth.vendor_name_printed, styles["vendor"]),
        *[Paragraph(line, styles["body"]) for line in vendor.address],
        Paragraph(f"Tax ID: {vendor.tax_id}", styles["small"]),
    ]
    meta = Table(_meta_rows(truth), colWidths=[32 * mm, 48 * mm])
    meta.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 8.5),
                ("FONT", (1, 0), (1, -1), "Helvetica", 8.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ]
        )
    )
    title = Paragraph(TITLES[truth.document_type], styles["title"])

    if template == "compact":
        # Key-value layout: everything stacked, no side-by-side header.
        story += [title, *vendor_block, Spacer(1, 4 * mm), meta]
    else:
        header = Table(
            [[vendor_block, [title, Spacer(1, 3 * mm), meta]]], colWidths=[95 * mm, 85 * mm]
        )
        header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(header)

    story.append(Spacer(1, 6 * mm))
    recipient = "Ship To" if truth.document_type == SyntheticDocType.DELIVERY_NOTE else "Bill To"
    story += _address_block(recipient, (truth.buyer_name, BUYER_CONTACT, *BUYER_ADDRESS), styles)
    story.append(Spacer(1, 6 * mm))
    story.append(_line_table(truth, locale, template))
    story.append(Spacer(1, 5 * mm))

    if truth.has_prices:
        story.append(_totals(truth, locale))
        story.append(Spacer(1, 8 * mm))
    if truth.document_type == SyntheticDocType.INVOICE:
        story.append(
            Paragraph(
                f"Please pay within {truth.payment_terms_days} days quoting the invoice number. "
                f"Bank: Example Commercial Bank, IBAN XX00 0000 {vendor.code} 0000 0000.",
                styles["small"],
            )
        )
    elif truth.document_type == SyntheticDocType.DELIVERY_NOTE:
        story.append(Paragraph("Received in good condition: ____________________", styles["body"]))
    else:
        story.append(
            Paragraph(
                "Please confirm this order and quote the PO number on all invoices.",
                styles["small"],
            )
        )
    story.append(Spacer(1, 4 * mm))
    story.append(
        Paragraph(
            "SYNTHETIC DOCUMENT - generated for testing; not a real transaction.", styles["small"]
        )
    )

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"{TITLES[truth.document_type].title()} {truth.number}",
        author=truth.vendor_name_canonical,
        creator="docintel synthetic generator",
        invariant=1,
    )
    document.build(story)
    return buffer.getvalue()
