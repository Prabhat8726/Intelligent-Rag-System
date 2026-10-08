"""JWT access tokens (HS256) with strict claim validation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from docintel.core.config import Settings
from docintel.db.models import Role

_REQUIRED_CLAIMS = ["exp", "iat", "nbf", "sub", "iss", "aud", "jti"]
_CLOCK_SKEW_LEEWAY_SECONDS = 10


class InvalidTokenError(Exception):
    """Token is malformed, expired, has a bad signature or wrong issuer/audience."""


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    subject: uuid.UUID
    role: Role
    token_id: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    expires_in_seconds: int


def create_access_token(
    *, user_id: uuid.UUID, role: Role, settings: Settings, now: datetime | None = None
) -> IssuedToken:
    issued_at = now or datetime.now(UTC)
    ttl = timedelta(minutes=settings.jwt_access_token_ttl_minutes)
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + ttl,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return IssuedToken(token=token, expires_in_seconds=int(ttl.total_seconds()))


def decode_access_token(token: str, settings: Settings) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            # Explicit allow-list: rejects "none" and algorithm-confusion attacks.
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=_CLOCK_SKEW_LEEWAY_SECONDS,
            options={"require": _REQUIRED_CLAIMS},
        )
        return AccessTokenClaims(
            subject=uuid.UUID(payload["sub"]),
            role=Role(payload["role"]),
            token_id=str(payload["jti"]),
            issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise InvalidTokenError(str(exc)) from exc
