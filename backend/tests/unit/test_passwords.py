from __future__ import annotations

import pytest

from docintel.auth.passwords import (
    PASSWORD_MAX_LENGTH,
    PasswordPolicyError,
    burn_verification_time,
    hash_password,
    validate_password_policy,
    verify_password,
)


def test_hash_is_argon2id_and_verifies() -> None:
    hashed = hash_password("a long passphrase value")
    assert hashed.startswith("$argon2id$")
    assert verify_password("a long passphrase value", hashed) == (True, None)


def test_wrong_password_fails() -> None:
    hashed = hash_password("a long passphrase value")
    assert verify_password("a long passphrase valuE", hashed) == (False, None)


def test_hashes_are_salted() -> None:
    assert hash_password("same password here") != hash_password("same password here")


def test_unknown_hash_format_fails_closed() -> None:
    assert verify_password("anything", "md5$deadbeef") == (False, None)


def test_burn_verification_time_does_not_raise() -> None:
    burn_verification_time("whatever the user typed")


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short", "at least"),
        ("x" * (PASSWORD_MAX_LENGTH + 1), "at most"),
        ("abababababababab", "repetitive"),
    ],
)
def test_password_policy_rejects(password: str, message: str) -> None:
    with pytest.raises(PasswordPolicyError, match=message):
        validate_password_policy(password)


def test_password_policy_accepts_passphrase() -> None:
    validate_password_policy("correct horse battery staple")
