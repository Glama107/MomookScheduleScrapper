"""Smoke test for the HTTP surface, with Momook itself stubbed out.

Run with:  python -m tests.test_app
"""

from __future__ import annotations

import os

os.environ.setdefault("MOMOOK_USERNAME", "test")
os.environ.setdefault("MOMOOK_PASSWORD", "test")
os.environ.setdefault("MOMOOK_FEED_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from momook_ics import app as app_module  # noqa: E402


class FakeBuilder:
    cached_at = 0.0
    last_error = None

    def get(self, *, force: bool = False) -> bytes:
        return b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"

    def start_background_refresh(self) -> None:
        pass

    def close(self) -> None:
        pass


def main() -> None:
    app_module.FeedBuilder = lambda settings: FakeBuilder()  # type: ignore[assignment]

    with TestClient(app_module.app) as client:
        wrong = client.get("/calendar/not-the-token.ics")
        assert wrong.status_code == 404, wrong.status_code

        ok = client.get("/calendar/test-token.ics")
        assert ok.status_code == 200, ok.status_code
        assert ok.headers["content-type"].startswith("text/calendar"), ok.headers
        assert ok.content.startswith(b"BEGIN:VCALENDAR")

        health = client.get("/healthz")
        assert health.status_code == 200 and health.json()["status"] == "ok"

    print("ok — token check, feed and health endpoints behave")


if __name__ == "__main__":
    main()
