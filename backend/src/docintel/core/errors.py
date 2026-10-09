"""Application error types.

Services raise these; the API layer renders them as RFC 9457 problem details. `detail` is
always safe to show to the caller - never put internal state, SQL or secrets in it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar


class AppError(Exception):
    status_code: ClassVar[int] = 500
    title: ClassVar[str] = "Internal Server Error"

    def __init__(
        self,
        detail: str,
        *,
        headers: Mapping[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})


class AuthenticationError(AppError):
    status_code = 401
    title = "Unauthorized"

    def __init__(self, detail: str = "Authentication required.") -> None:
        super().__init__(detail, headers={"WWW-Authenticate": "Bearer"})


class PermissionDeniedError(AppError):
    status_code = 403
    title = "Forbidden"

    def __init__(self, detail: str = "You do not have permission to perform this action.") -> None:
        super().__init__(detail)


class NotFoundError(AppError):
    status_code = 404
    title = "Not Found"


class ConflictError(AppError):
    status_code = 409
    title = "Conflict"


class ServiceUnavailableError(AppError):
    status_code = 503
    title = "Service Unavailable"


class PayloadTooLargeError(AppError):
    status_code = 413
    title = "Content Too Large"


class UnsupportedMediaTypeError(AppError):
    status_code = 415
    title = "Unsupported Media Type"


class UnprocessableContentError(AppError):
    """Well-formed request whose content cannot be accepted (corrupt, encrypted, too many pages)."""

    status_code = 422
    title = "Unprocessable Content"


class TooManyRequestsError(AppError):
    status_code = 429
    title = "Too Many Requests"
