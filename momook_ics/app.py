"""FastAPI service exposing the schedule as a subscribable .ics feed."""

from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response

from .config import get_settings
from .feed import FeedBuilder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("momook_ics")

settings = get_settings()
builder: FeedBuilder | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global builder
    settings.require_credentials()
    if not settings.feed_token:
        raise RuntimeError(
            "MOMOOK_FEED_TOKEN is required: it is the only thing protecting your "
            "schedule at a public URL. Generate one with `openssl rand -hex 24`."
        )
    builder = FeedBuilder(settings)
    log.info(
        "Serving %s → /calendar/%s.ics (window: -%dd/+%dd, cache %ds)",
        settings.base_url,
        "*" * 8,
        settings.days_past,
        settings.days_future,
        settings.cache_ttl,
    )
    try:
        yield
    finally:
        builder.close()


app = FastAPI(title="Momook iCal feed", lifespan=lifespan, docs_url=None, redoc_url=None)


def _check_token(token: str) -> None:
    # Constant-time compare so the token cannot be recovered by timing the 404s.
    if not hmac.compare_digest(token, settings.feed_token):
        raise HTTPException(status_code=404, detail="Not found")


@app.get("/healthz")
def healthz() -> dict:
    assert builder is not None
    age = time.monotonic() - builder.cached_at if builder.cached_at else None
    return {
        "status": "ok",
        "cache_age_seconds": round(age) if age is not None else None,
        "last_error": builder.last_error,
    }


@app.get("/calendar/{token}.ics")
def calendar(token: str, refresh: bool = False) -> Response:
    _check_token(token)
    assert builder is not None
    try:
        document = builder.get(force=refresh)
    except Exception as exc:  # noqa: BLE001 - no cached copy to fall back on
        log.error("Cannot produce a calendar: %s", exc)
        raise HTTPException(status_code=503, detail="Momook is unreachable") from exc

    return Response(
        content=document,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="momook.ics"',
            "Cache-Control": f"public, max-age={settings.cache_ttl}",
        },
    )


@app.get("/")
def index() -> dict:
    return {"service": "momook-ics", "feed": "/calendar/<token>.ics"}


def main() -> None:
    import uvicorn

    uvicorn.run(
        "momook_ics.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
