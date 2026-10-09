from __future__ import annotations

import pytest
from pydantic import ValidationError

from docintel.core.config import Environment, LogFormat
from tests.conftest import PRODUCTION_SECRET as STRONG_SECRET
from tests.conftest import make_settings


def test_cors_origins_are_split_from_comma_separated_string() -> None:
    settings = make_settings(cors_allowed_origins="http://a.test, http://b.test,")
    assert settings.cors_allowed_origins == ["http://a.test", "http://b.test"]


def test_database_url_must_use_psycopg_driver() -> None:
    with pytest.raises(ValidationError, match="psycopg"):
        make_settings(database_url="postgresql://u:p@localhost/db")


def test_short_jwt_secret_is_rejected_in_every_environment() -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        make_settings(jwt_secret_key="too-short")


def test_placeholder_secret_allowed_locally_but_refused_in_production() -> None:
    placeholder = "replace_with_a_long_random_secret_value_000000"
    assert make_settings(app_env="local", jwt_secret_key=placeholder).app_env == Environment.LOCAL
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(app_env="production", jwt_secret_key=placeholder)


def test_wildcard_cors_refused_outside_local() -> None:
    with pytest.raises(ValidationError, match="CORS"):
        make_settings(app_env="staging", jwt_secret_key=STRONG_SECRET, cors_allowed_origins="*")


def test_environment_derived_defaults() -> None:
    local = make_settings(app_env="local")
    production = make_settings(app_env="production", jwt_secret_key=STRONG_SECRET)
    assert local.effective_log_format == LogFormat.CONSOLE
    assert local.effective_api_docs_enabled is True
    assert local.hsts_enabled is False
    assert production.effective_log_format == LogFormat.JSON
    assert production.effective_api_docs_enabled is False
    assert production.hsts_enabled is True


def test_review_sla_hours_keep_defaults_for_omitted_priorities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_SLA_HOURS", '{"URGENT": 2}')
    settings = make_settings()
    assert settings.review_sla_hours == {"URGENT": 2, "HIGH": 24, "NORMAL": 72, "LOW": 168}
    with pytest.raises(ValidationError, match="REVIEW_SLA_HOURS"):
        make_settings(review_sla_hours={"LOW": 0})
    with pytest.raises(ValidationError):
        make_settings(review_sla_hours={"SOMEDAY": 5})


def test_blank_gemini_key_is_treated_as_missing() -> None:
    assert make_settings(gemini_api_key="   ").gemini_api_key is None


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = make_settings(gemini_api_key="AIza-very-secret-value")
    assert "AIza-very-secret-value" not in repr(settings)
    assert STRONG_SECRET not in repr(make_settings(jwt_secret_key=STRONG_SECRET))
