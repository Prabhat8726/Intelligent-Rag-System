"""Demo vendor master (`docintel seed`): the fictional vendors of the synthetic generator.

Only canonical names, tax IDs, currencies and payment terms are seeded - not the printed name
variants, which normalization has to resolve on its own (that is what the evaluation measures).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import SYSTEM_REQUEST
from docintel.db.models import Vendor
from docintel.synthetic.catalog import LOCALES, VENDORS
from docintel.vendors.service import VendorData, VendorService


def demo_vendors() -> list[VendorData]:
    return [
        VendorData(
            canonical_name=vendor.name,
            tax_id=vendor.tax_id,
            default_currency=LOCALES[vendor.country].currency,
            payment_terms_days=vendor.payment_terms_days,
        )
        for vendor in VENDORS
    ]


async def seed_demo_vendors(session: AsyncSession) -> tuple[list[str], list[str]]:
    """Idempotent: returns (created, existing) canonical names."""
    existing = set(await session.scalars(select(Vendor.canonical_name)))
    created: list[str] = []
    service = VendorService(session)
    for data in demo_vendors():
        if data.canonical_name in existing:
            continue
        await service.create(None, data, SYSTEM_REQUEST)
        created.append(data.canonical_name)
    return created, sorted(existing & {data.canonical_name for data in demo_vendors()})
