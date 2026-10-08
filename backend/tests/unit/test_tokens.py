from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from docintel.auth.tokens import InvalidTokenError, create_access_token, decode_access_token
from docintel.db.models import Role
from tests.conftest import TEST_JWT_SECRET, make_settings

SETTINGS = make_settings()


def _b64(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "role": "ADMIN",
        "iss": SETTINGS.jwt_issuer,
        "aud": SETTINGS.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "jti": "abc",
    }
    claims.update(overrides)
    return claims


def test_round_trip() -> None:
    user_id = uuid.uuid4()
    issued = create_access_token(user_id=user_id, role=Role.REVIEWER, settings=SETTINGS)
    claims = decode_access_token(issued.token, SETTINGS)
    assert claims.subject == user_id
    assert claims.role == Role.REVIEWER
    assert issued.expires_in_seconds == SETTINGS.jwt_access_token_ttl_minutes * 60
    assert claims.expires_at - claims.issued_at == timedelta(seconds=issued.expires_in_seconds)


def test_expired_token_rejected() -> None:
    issued = create_access_token(
        user_id=uuid.uuid4(),
        role=Role.ADMIN,
        settings=SETTINGS,
        now=datetime.now(UTC) - timedelta(hours=2),
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(issued.token, SETTINGS)


def test_wrong_signing_key_rejected() -> None:
    forged = jwt.encode(_claims(), "another-secret-key-of-sufficient-length-123", algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        decode_access_token(forged, SETTINGS)


def test_alg_none_rejected() -> None:
    payload = {
        key: int(value.timestamp()) if isinstance(value, datetime) else value
        for key, value in _claims().items()
    }
    unsigned = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(payload)}."
    with pytest.raises(InvalidTokenError):
        decode_access_token(unsigned, SETTINGS)


@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
def test_other_hmac_algorithm_rejected() -> None:
    token = jwt.encode(_claims(), TEST_JWT_SECRET, algorithm="HS512")
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, SETTINGS)


@pytest.mark.parametrize(
    "overrides", [{"aud": "someone-else"}, {"iss": "evil-issuer"}, {"role": "SUPERUSER"}]
)
def test_wrong_audience_issuer_or_role_rejected(overrides: dict[str, object]) -> None:
    token = jwt.encode(_claims(**overrides), TEST_JWT_SECRET, algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, SETTINGS)


@pytest.mark.parametrize("missing", ["exp", "jti", "sub", "nbf"])
def test_missing_required_claim_rejected(missing: str) -> None:
    claims = _claims()
    del claims[missing]
    token = jwt.encode(claims, TEST_JWT_SECRET, algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, SETTINGS)


def test_garbage_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token("not-a-jwt", SETTINGS)
