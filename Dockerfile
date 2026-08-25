FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml ./
COPY momook_ics ./momook_ics
RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir .


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    MOMOOK_PORT=8080 \
    # glibc's malloc gives each thread its own arena (up to 8x the CPU count),
    # which fragments the heap of a small multi-threaded service like this one.
    # Capping it keeps allocations pooled instead of scattered across arenas
    # that never fully empty.
    MALLOC_ARENA_MAX=2 \
    # CPython's own small-object allocator (pymalloc) keeps its arenas until
    # they are completely empty, which glibc's malloc_trim can't see or force —
    # so the long-lived objects each refresh leaves behind (cached calendars,
    # parsed events) fragment the heap a little more every cycle. Routing
    # everything straight to glibc lets the periodic malloc_trim() actually
    # reclaim what a refresh's year-of-JSON parse frees.
    PYTHONMALLOC=malloc

# tzdata backs the ZoneInfo lookups used to build VTIMEZONE blocks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin momook

COPY --from=build /venv /venv

USER momook
EXPOSE 8080

# No shell in the entrypoint: signals reach uvicorn directly.
CMD ["python", "-m", "momook_ics.app"]
