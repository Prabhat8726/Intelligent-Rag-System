"""Vendor resolution against master data (canonical names only, as seeded)."""

from __future__ import annotations

import uuid

import pytest

from docintel.fields.normalize import organization_key
from docintel.fields.vendors import StaticVendorDirectory, VendorRecord, best_match, tax_id_key
from docintel.synthetic.catalog import LOCALES, VENDORS

RECORDS = [
    VendorRecord(
        uuid.uuid4(),
        vendor.name,
        organization_key(vendor.name),
        (),
        tax_id_key(vendor.tax_id),
        LOCALES[vendor.country].currency,
        vendor.payment_terms_days,
    )
    for vendor in VENDORS
]


@pytest.mark.parametrize(
    ("vendor", "variant"),
    [(vendor, variant) for vendor in VENDORS for variant in vendor.name_variants],
)
def test_every_printed_variant_resolves_to_its_vendor(vendor: object, variant: str) -> None:
    match = best_match(RECORDS, variant, None)
    assert match is not None, variant
    assert match.canonical_name == vendor.name  # type: ignore[attr-defined]
    assert match.method == "name"


def test_tax_id_is_decisive_and_aliases_are_exact() -> None:
    match = best_match(RECORDS, "Some Other Name", "US 47-2917735")
    assert match is not None
    assert (match.canonical_name, match.method, match.score) == (
        "Kestrel Industrial Supply Inc.",
        "tax_id",
        100.0,
    )
    aliased = VendorRecord(uuid.uuid4(), "Northwind Traders", "northwind traders", ("nwt",))
    match = best_match([aliased], "NWT", None)
    assert match is not None
    assert match.method == "alias"


def test_unknown_names_stay_unmatched() -> None:
    assert best_match(RECORDS, "Completely Different Holdings", None) is None
    assert best_match(RECORDS, None, None) is None
    assert best_match([], "Kestrel Industrial Supply Inc.", None) is None


async def test_static_directory() -> None:
    directory = StaticVendorDirectory(RECORDS)
    assert await directory.has_vendors()
    match = await directory.match("ALTAMIRA COMPONENTS", None)
    assert match is not None
    assert match.default_currency == "EUR"
    assert not await StaticVendorDirectory().has_vendors()
