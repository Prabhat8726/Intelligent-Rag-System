"""Fictional master data for synthetic documents (no real companies or people)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Locale:
    currency: str
    symbol: str
    tax_label: str
    tax_rate: Decimal
    decimal_separator: str
    thousands_separator: str
    symbol_after: bool
    date_format: str  # strftime pattern used on documents


LOCALES: dict[str, Locale] = {
    "US": Locale("USD", "$", "Sales tax", Decimal("0.0825"), ".", ",", False, "%m/%d/%Y"),
    "DE": Locale("EUR", "€", "MwSt", Decimal("0.19"), ",", ".", True, "%d.%m.%Y"),
    "GB": Locale("GBP", "£", "VAT", Decimal("0.20"), ".", ",", False, "%d/%m/%Y"),
    # "Rs." rather than U+20B9: the PDF base-14 fonts have no rupee glyph.
    "IN": Locale("INR", "Rs.", "GST", Decimal("0.18"), ".", ",", False, "%d-%m-%Y"),
}


@dataclass(frozen=True, slots=True)
class Item:
    sku: str
    description: str
    unit: str
    base_price: Decimal


@dataclass(frozen=True, slots=True)
class Vendor:
    code: str
    name: str
    # Alternative spellings that should normalize to the same vendor (Module 8 test data).
    name_variants: tuple[str, ...]
    address: tuple[str, ...]
    tax_id: str
    country: str
    payment_terms_days: int
    items: tuple[Item, ...]


INDUSTRIAL = (
    Item("BRG-6204", "Deep groove ball bearing 6204-2RS", "pcs", Decimal("4.85")),
    Item("VLV-BL050", "Brass ball valve DN50 PN25", "pcs", Decimal("38.40")),
    Item("GSK-150A", "Graphite gasket 150 mm", "pcs", Decimal("6.20")),
    Item("HYD-HS12", "Hydraulic hose 1/2in, per metre", "m", Decimal("9.75")),
    Item("FLT-AIR3", "Compressor air filter element", "pcs", Decimal("27.10")),
    Item("BLT-M10", "Hex bolt M10x40 zinc, box of 100", "box", Decimal("14.60")),
)
OFFICE = (
    Item("PAP-A4-80", "Copy paper A4 80gsm, ream", "ream", Decimal("4.10")),
    Item("TNR-K310", "Toner cartridge black K310", "pcs", Decimal("72.50")),
    Item("BND-25", "Lever arch binder 75 mm", "pcs", Decimal("3.35")),
    Item("CHR-ERG2", "Ergonomic office chair", "pcs", Decimal("189.00")),
    Item("PEN-BLU50", "Ballpoint pens blue, pack of 50", "pack", Decimal("8.90")),
)
PACKAGING = (
    Item("CTN-4030", "Corrugated carton 400x300x300", "pcs", Decimal("0.92")),
    Item("STF-500", "Stretch film 500 mm x 300 m", "roll", Decimal("15.40")),
    Item("PLT-EUR", "Euro pallet 1200x800", "pcs", Decimal("11.80")),
    Item("TAP-48", "Packing tape 48 mm, roll", "roll", Decimal("1.45")),
)
TOOLS = (
    Item("DRL-HSS8", "HSS drill bit 8 mm, set of 10", "set", Decimal("21.30")),
    Item("CAL-150D", "Digital caliper 150 mm", "pcs", Decimal("34.90")),
    Item("TRQ-2050", "Torque wrench 20-100 Nm", "pcs", Decimal("96.00")),
    Item("GLV-CUT5", "Cut-resistant gloves level 5, pair", "pair", Decimal("6.75")),
)

VENDORS: tuple[Vendor, ...] = (
    Vendor(
        "KIS",
        "Kestrel Industrial Supply Inc.",
        ("KESTREL INDUSTRIAL SUPPLY", "Kestrel Industrial Supply, Inc", "Kestrel Ind. Supply Inc."),
        ("1840 Foundry Road", "Dayton, OH 45402", "United States"),
        "US-47-2917735",
        "US",
        30,
        INDUSTRIAL,
    ),
    Vendor(
        "BOS",
        "Bluepeak Office Solutions LLC",
        ("BLUEPEAK OFFICE SOLUTIONS", "Bluepeak Office Solutions, L.L.C."),
        ("77 Larkspur Avenue, Suite 300", "Austin, TX 78701", "United States"),
        "US-83-5521094",
        "US",
        45,
        OFFICE,
    ),
    Vendor(
        "ACG",
        "Altamira Components GmbH",
        ("ALTAMIRA COMPONENTS", "Altamira Components G.m.b.H."),
        ("Industriestraße 12", "70565 Stuttgart", "Germany"),
        "DE298374615",
        "DE",
        30,
        INDUSTRIAL,
    ),
    Vendor(
        "HPP",
        "Harbor & Pine Packaging Ltd.",
        ("Harbor and Pine Packaging Ltd", "HARBOR & PINE PACKAGING LIMITED"),
        ("Unit 4, Quayside Trading Estate", "Bristol BS1 6XN", "United Kingdom"),
        "GB 284 7712 09",
        "GB",
        60,
        PACKAGING,
    ),
    Vendor(
        "SPT",
        "Sundaram Precision Tools Pvt. Ltd.",
        ("SUNDARAM PRECISION TOOLS PRIVATE LIMITED", "Sundaram Precision Tools Pvt Ltd"),
        ("Plot 22, SIDCO Industrial Estate", "Coimbatore 641021", "India"),
        "33AABCS1429B1ZQ",
        "IN",
        30,
        TOOLS,
    ),
)

BUYER_NAME = "Meridian Manufacturing Co."
BUYER_ADDRESS = ("500 Commerce Parkway", "Columbus, OH 43215", "United States")
BUYER_CONTACT = "Procurement Department"
