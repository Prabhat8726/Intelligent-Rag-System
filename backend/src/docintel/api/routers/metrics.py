"""Prometheus scrape endpoint (Module 24, ADR-073).

`GET /metrics` returns this process's counters and histograms followed by the gauges read
from the database. It sits outside `/api/` and the web container does not proxy it: Prometheus
scrapes each API replica on the internal network. With METRICS_TOKEN set (required in staging
and production) the scraper must send it as a bearer token; the comparison is constant-time.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from prometheus_client import REGISTRY, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_PLAIN_0_0_4

from docintel.api.deps import SessionDep, SettingsDep
from docintel.core.errors import AuthenticationError
from docintel.core.metrics import bearer_matches
from docintel.observability import snapshot

router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, session: SessionDep, settings: SettingsDep) -> Response:
    if settings.metrics_token is not None and not bearer_matches(
        request.headers.get("authorization"), settings.metrics_token.get_secret_value()
    ):
        raise AuthenticationError("A valid metrics token is required.")
    families = await snapshot.collect(session, settings)
    body = generate_latest(REGISTRY) + snapshot.exposition(families)
    return Response(body, media_type=CONTENT_TYPE_PLAIN_0_0_4)
