"""Identity tables: departments and users."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String, text
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
