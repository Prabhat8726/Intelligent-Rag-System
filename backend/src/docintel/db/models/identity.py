"""Identity tables: departments, users and browser-session refresh tokens."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docintel.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Role(StrEnum):
    """Closed set of roles. Capabilities are mapped in `docintel.auth.permissions`."""

    ADMIN = "ADMIN"
    MANAGER = "MANAGER"
    ANALYST = "ANALYST"
    REVIEWER = "REVIEWER"
    VIEWER = "VIEWER"


class Department(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "departments"

    name: Mapped[str] = mapped_column(String(100), unique=True)

    users: Mapped[list[User]] = relationship(back_populates="department")


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("failed_login_attempts >= 0", name="failed_login_attempts_non_negative"),
        Index("ix_users_department_id", "department_id"),
        Index("ix_users_role", "role"),
    )

    email: Mapped[str] = mapped_column(String(320), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(
        Enum(
            Role,
            name="role",
            native_enum=False,
            create_constraint=True,
            length=20,
            values_callable=lambda enum: [member.value for member in enum],
            validate_strings=True,
        )
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    failed_login_attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    locked_until: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]

    department: Mapped[Department | None] = relationship(back_populates="users", lazy="joined")


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    """A browser session's refresh token (ADR-063). Only its SHA-256 is stored.

    Every sign-in starts a family; each refresh replaces the token with a new one of the same
    family. Presenting a replaced or revoked token is treated as theft: the family is revoked.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
        CheckConstraint("expires_at > created_at", name="expires_after_creation"),
        CheckConstraint("expires_at <= session_expires_at", name="within_session"),
        CheckConstraint(
            "revoked_at IS NULL OR revoke_reason IS NOT NULL", name="revocation_has_reason"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID]
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime]  # idle limit: unused this long, the session ends
    session_expires_at: Mapped[datetime]  # absolute limit of the family (sign in again)
    replaced_at: Mapped[datetime | None]  # rotated: a newer token of the family exists
    revoked_at: Mapped[datetime | None]
    revoke_reason: Mapped[str | None] = mapped_column(String(30))
