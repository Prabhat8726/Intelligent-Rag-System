"""Reusable column types."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum


def str_enum(enum_cls: type[StrEnum], name: str, length: int = 20) -> Enum:
    """VARCHAR + named CHECK constraint (no native PG enum: adding values needs no type lock)."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda enum: [member.value for member in enum],
        validate_strings=True,
    )
