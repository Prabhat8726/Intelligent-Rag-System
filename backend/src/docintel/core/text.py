"""Validated text types shared by request bodies and agent tool inputs."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def plain_text(value: str) -> str:
    """Search and question text: no control characters (PostgreSQL text cannot hold NUL)."""
    if _CONTROL.search(value):
        msg = "must not contain control characters"
        raise ValueError(msg)
    if not value.strip():
        msg = "must not be blank"
        raise ValueError(msg)
    return value.strip()


QueryText = Annotated[str, AfterValidator(plain_text)]
