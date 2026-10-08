from __future__ import annotations

import json
import logging

import pytest

from docintel.core.config import LogFormat
from docintel.core.logging import REDACTED, configure_logging, get_logger, redact_sensitive


def _redact(event: dict[str, object]) -> dict[str, object]:
    return dict(redact_sensitive(None, "info", event))


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "new_password",
        "password_hash",
        "api_key",
        "gemini_api_key",
        "x-api-key",
        "authorization",
        "access_token",
        "refresh_token",
        "token",
        "cookie",
        "set-cookie",
        "client_secret",
        "jwt_secret_key",
        "credentials",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    assert _redact({"event": "x", key: "value"})[key] == REDACTED


@pytest.mark.parametrize(
    "key", ["input_tokens", "output_tokens", "token_type", "tokens_used", "email", "user_id"]
)
def test_non_sensitive_keys_are_kept(key: str) -> None:
    assert _redact({"event": "x", key: 42})[key] == 42


def test_nested_structures_and_bearer_values_are_redacted() -> None:
    event = _redact(
        {
            "event": "call",
            "headers": {"Authorization": "Bearer abc.def.ghi", "accept": "json"},
            "items": [{"password": "p"}, "Bearer xyz"],
            "message": "sent bearer eyJhbGciOi.payload.sig to upstream",
        }
    )
    assert event["headers"] == {"Authorization": REDACTED, "accept": "json"}
    assert event["items"] == [{"password": REDACTED}, f"Bearer {REDACTED}"]
    assert "eyJhbGciOi" not in str(event["message"])


def test_json_logs_are_redacted_end_to_end(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON)
    get_logger("test").info("login.attempt", email="a@b.test", password="hunter2hunter2")
    logging.getLogger("stdlib").warning("plain %s", "message")
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["password"] == REDACTED
    assert lines[0]["email"] == "a@b.test"
    assert lines[0]["level"] == "info"
    assert lines[1]["event"] == "plain message"
    assert "hunter2hunter2" not in json.dumps(lines)
