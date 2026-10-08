"""Password hashing (argon2id via pwdlib) and password policy.

Argon2 is intentionally CPU/memory expensive; async callers use the `*_async` helpers so the
event loop is never blocked by hashing.
"""

from __future__ import annotations

import asyncio
import secrets
from functools import lru_cache

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher

# NIST SP 800-63B: favour length over composition rules; cap length to bound hashing cost.
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 256


class PasswordPolicyError(ValueError):
    pass


_hasher = PasswordHash((Argon2Hasher(),))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Return (is_valid, new_hash_if_parameters_changed). Unknown hash formats fail closed."""
    try:
        return _hasher.verify_and_update(password, password_hash)
    except UnknownHashError:
        return False, None


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(32))


def burn_verification_time(password: str) -> None:
    """Spend the same work as a real verification (used when the user does not exist)."""
    _hasher.verify(password, _dummy_hash())


async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, password_hash: str) -> tuple[bool, str | None]:
    return await asyncio.to_thread(verify_password, password, password_hash)


async def burn_verification_time_async(password: str) -> None:
    await asyncio.to_thread(burn_verification_time, password)


def validate_password_policy(password: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        msg = f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
        raise PasswordPolicyError(msg)
    if len(password) > PASSWORD_MAX_LENGTH:
        msg = f"Password must be at most {PASSWORD_MAX_LENGTH} characters."
        raise PasswordPolicyError(msg)
    if len(set(password)) < 4:
        msg = "Password is too repetitive."
        raise PasswordPolicyError(msg)
