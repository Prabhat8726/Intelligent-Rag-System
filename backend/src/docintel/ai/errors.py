"""Provider-neutral error taxonomy. Callers branch on these types, never on SDK internals."""

from __future__ import annotations

from collections.abc import Sequence


class ProviderError(Exception):
    retryable: bool = False

    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderConfigurationError(ProviderError):
    """Missing/invalid API key, unknown model, permission denied. Fix configuration, don't retry."""


class ProviderRequestError(ProviderError):
    """The provider rejected the request as invalid (a bug or unsupported parameter)."""


class ProviderRateLimitError(ProviderError):
    """Quota or rate limit exceeded (HTTP 429). Retry later."""

    retryable = True


class ProviderUnavailableError(ProviderError):
    """Timeout, network failure or provider-side 5xx. Retry later."""

    retryable = True


class ProviderResponseError(ProviderError):
    """The call succeeded but produced no usable output (blocked, empty, wrong shape)."""


class StructuredOutputError(ProviderResponseError):
    """Model output was not valid JSON for the requested schema.

    `raw_text` is kept (truncated) so the caller can attempt one repair round-trip.
    """

    RAW_TEXT_LIMIT = 4000

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        raw_text: str,
        validation_errors: Sequence[str] = (),
    ) -> None:
        super().__init__(message, provider=provider)
        self.raw_text = raw_text[: self.RAW_TEXT_LIMIT]
        self.validation_errors = list(validation_errors)
