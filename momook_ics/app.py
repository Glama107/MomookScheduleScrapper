"""FastAPI service exposing each configured schedule as a subscribable .ics feed."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response

from .config import get_settings
from .feed import FeedBuilder, FeedRegistry

log = logging.getLogger("momook_ics")


def configure_logging(verbose: bool = False) -> None:
    """Set up root logging. Both entry points call this, exactly once."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


settings = get_settings()
registry: FeedRegistry | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global registry
    settings.require_serving()
    registry = FeedRegistry(settings)
    log.info(
        "Serving %s → %d account(s): %s (window: -%dd/+%dd, refresh %ds each, %ds apart)",
        settings.base_url,
        len(registry),
        ", ".join(builder.label for builder in registry.builders),
        settings.days_past,
        settings.days_future,
        settings.cache_ttl,
        settings.refresh_gap,
    )
    registry.start()
    try:
        yield
    finally:
        registry.close()


app = FastAPI(title="Momook iCal feed", lifespan=lifespan, docs_url=None, redoc_url=None)


def _feed_for(token: str) -> FeedBuilder:
    assert registry is not None
    builder = registry.find(token)
    if builder is None:
        raise HTTPException(status_code=404, detail="Not found")
    return builder


@app.get("/healthz")
async def healthz() -> dict:
    assert registry is not None
    accounts = registry.status()
    return {
        "status": "ok" if all(a["last_error"] is None for a in accounts) else "degraded",
        "accounts": accounts,
    }


@app.get("/calendar/{token}.ics")
def calendar(token: str) -> Response:
    """Always the cached document. Refreshing is the background thread's job —
    a request must never be able to trigger a multi-minute rebuild."""
    builder = _feed_for(token)
    document = builder.get()
    if document is None:
        # Only before the first refresh of this account completes, or after it
        # has failed every time since start-up.
        log.warning("[%s] No calendar to serve yet: %s", builder.label, builder.last_error)
        raise HTTPException(status_code=503, detail="Calendar not built yet")

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

    # The container entrypoint comes through here. Say what is missing in one
    # line and stop, rather than failing inside the lifespan with a traceback.
    try:
        settings.require_serving()
    except RuntimeError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from None

    uvicorn.run(
        "momook_ics.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
