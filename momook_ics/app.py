"""FastAPI service exposing the schedule as a subscribable .ics feed."""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response

from .config import get_settings
from .feed import FeedBuilder

log = logging.getLogger("momook_ics")


def configure_logging(verbose: bool = False) -> None:
    """Set up root logging. Both entry points call this, exactly once."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


settings = get_settings()
builder: FeedBuilder | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global builder
    settings.require_serving()
    builder = FeedBuilder(settings)
    log.info(
        "Serving %s → /calendar/%s.ics (window: -%dd/+%dd, refresh %ds)",
        settings.base_url,
        "*" * 8,
        settings.days_past,
        settings.days_future,
        settings.cache_ttl,
    )
    builder.start_background_refresh()
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
async def healthz() -> dict:
    assert builder is not None
    age = builder.cache_age_seconds
    return {
        "status": "ok",
        "cache_age_seconds": round(age) if age is not None else None,
        "last_error": builder.last_error,
    }


@app.get("/calendar/{token}.ics")
def calendar(token: str) -> Response:
    """Always the cached document. Refreshing is the background thread's job —
    a request must never be able to trigger a multi-minute rebuild."""
    _check_token(token)
    assert builder is not None
    try:
        document = builder.get()
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
async def index() -> dict:
    return {"service": "momook-ics", "feed": "/calendar/<token>.ics"}


def main() -> None:
    import uvicorn

    configure_logging()

    uvicorn.run(
        "momook_ics.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
