"""Vendor master data endpoints (normalization target for extracted vendor names)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from docintel.api.deps import RequestMetaDep, SessionDep, require_permission
from docintel.api.schemas.common import PROBLEM_RESPONSES, ProblemDetail
from docintel.api.schemas.vendors import VendorCreate, VendorPage, VendorRead, VendorUpdate
from docintel.auth.permissions import Permission
from docintel.db.models import User
from docintel.vendors.service import VendorData, VendorService

router = APIRouter(
    prefix="/vendors",
    tags=["vendors"],
    responses={
        **PROBLEM_RESPONSES,
        404: {"model": ProblemDetail, "description": "Not found"},
    },
)

Reader = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_READ))]
Manager = Annotated[User, Depends(require_permission(Permission.VENDORS_MANAGE))]


@router.get("", response_model=VendorPage, summary="List or search the vendor master")
async def list_vendors(
    user: Reader,
    session: SessionDep,
    q: Annotated[
        str | None, Query(max_length=200, description="Name (fuzzy), or exact tax ID")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> VendorPage:
    items, total = await VendorService(session).list(q, limit=limit, offset=offset)
    return VendorPage(
        items=[VendorRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{vendor_id}", response_model=VendorRead, summary="Vendor detail")
async def get_vendor(vendor_id: uuid.UUID, user: Reader, session: SessionDep) -> VendorRead:
    return VendorRead.model_validate(await VendorService(session).get(vendor_id))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=VendorRead,
    summary="Add a vendor (canonical name, aliases, tax ID, defaults)",
    responses={409: {"model": ProblemDetail, "description": "Name already exists"}},
)
async def create_vendor(
    body: VendorCreate,
    response: Response,
    user: Manager,
    session: SessionDep,
    meta: RequestMetaDep,
) -> VendorRead:
    vendor = await VendorService(session).create(
        user,
        VendorData(
            canonical_name=body.canonical_name,
            aliases=body.aliases,
            tax_id=body.tax_id,
            default_currency=body.default_currency,
            payment_terms_days=body.payment_terms_days,
            is_active=body.is_active,
        ),
        meta,
    )
    response.headers["Location"] = f"/api/v1/vendors/{vendor.id}"
    return VendorRead.model_validate(vendor)


@router.patch(
    "/{vendor_id}",
    response_model=VendorRead,
    summary="Update a vendor (e.g. add an alias a reviewer confirmed)",
    responses={409: {"model": ProblemDetail, "description": "Name already exists"}},
)
async def update_vendor(
    vendor_id: uuid.UUID,
    body: VendorUpdate,
    user: Manager,
    session: SessionDep,
    meta: RequestMetaDep,
) -> VendorRead:
    service = VendorService(session)
    current = await service.get(vendor_id)
    fields = body.model_fields_set
    data = VendorData(
        canonical_name=body.canonical_name or current.canonical_name,
        aliases=body.aliases
        if "aliases" in fields and body.aliases is not None
        else current.aliases,
        tax_id=body.tax_id if "tax_id" in fields else current.tax_id,
        default_currency=(
            body.default_currency if "default_currency" in fields else current.default_currency
        ),
        payment_terms_days=(
            body.payment_terms_days
            if "payment_terms_days" in fields
            else current.payment_terms_days
        ),
        is_active=body.is_active if body.is_active is not None else current.is_active,
    )
    return VendorRead.model_validate(await service.update(user, vendor_id, data, meta))
