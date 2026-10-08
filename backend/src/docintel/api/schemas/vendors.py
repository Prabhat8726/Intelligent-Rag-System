"""Vendor master API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from docintel.api.schemas.common import RequestModel, ResponseModel

_CURRENCY = r"^[A-Z]{3}$"


class VendorSummary(ResponseModel):
    id: uuid.UUID
    canonical_name: str


class VendorRead(VendorSummary):
    aliases: list[str]
    tax_id: str | None
    default_currency: str | None
    payment_terms_days: int | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class VendorCreate(RequestModel):
    canonical_name: str = Field(min_length=2, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    tax_id: str | None = Field(default=None, max_length=64)
    default_currency: str | None = Field(default=None, pattern=_CURRENCY)
    payment_terms_days: int | None = Field(default=None, ge=0, le=3650)
    is_active: bool = True


class VendorUpdate(RequestModel):
    """Fields left out keep their current value."""

    canonical_name: str | None = Field(default=None, min_length=2, max_length=300)
    aliases: list[str] | None = Field(default=None, max_length=50)
    tax_id: str | None = Field(default=None, max_length=64)
    default_currency: str | None = Field(default=None, pattern=_CURRENCY)
    payment_terms_days: int | None = Field(default=None, ge=0, le=3650)
    is_active: bool | None = None


class VendorPage(ResponseModel):
    items: list[VendorRead]
    total: int
    limit: int
    offset: int
