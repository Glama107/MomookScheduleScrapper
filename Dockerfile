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
    MOMOOK_PORT=8080

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
