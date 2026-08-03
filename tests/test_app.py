"""Smoke test for the HTTP surface, with Momook itself stubbed out.

Run with:  python -m tests.test_app
"""

from __future__ import annotations

import os
import tempfile

# The settings resolve ".env" relative to the working directory; step somewhere
# empty so a real one cannot add accounts to this roster.
os.chdir(tempfile.mkdtemp(prefix="momook-test-app-"))

os.environ["MOMOOK_USERNAME"] = "test"
os.environ["MOMOOK_PASSWORD"] = "test"
os.environ["MOMOOK_FEED_TOKEN"] = "test-token"
os.environ["MOMOOK_ACCOUNT_2_USERNAME"] = "marie"
os.environ["MOMOOK_ACCOUNT_2_PASSWORD"] = "s3cret"
os.environ["MOMOOK_ACCOUNT_2_FEED_TOKEN"] = "marie-token"
os.environ["MOMOOK_ACCOUNT_2_LABEL"] = "Marie"

from fastapi.testclient import TestClient  # noqa: E402

from momook_ics import app as app_module  # noqa: E402
from momook_ics import feed as feed_module  # noqa: E402

# Accounts whose first refresh has not landed. The real registry is exercised —
# token lookup, status, shutdown — only the per-account feeds are faked.
COLD: set = set()


class FakeBuilder:
    """Stands in for a real feed: no Momook, no fetching."""

    def __init__(self, account, settings) -> None:
        self.account = account
        self.label = account.label
        self.cold = account.label in COLD
        self.last_error = "MomookAuthError: rejected" if self.cold else None
        self.cache_age_seconds = None if self.cold else 12.0
        self._document = f"BEGIN:VCALENDAR\r\nX-WR-CALNAME:{account.label}\r\nEND:VCALENDAR\r\n"

    def get(self) -> bytes | None:
        return None if self.cold else self._document.encode("utf-8")

    def refresh(self) -> None:
        pass  # what the registry's thread calls; there is nothing to fetch

    def close(self) -> None:
        pass


def main() -> None:
    feed_module.FeedBuilder = FakeBuilder  # type: ignore[assignment]

    with TestClient(app_module.app) as client:
        wrong = client.get("/calendar/not-the-token.ics")
        assert wrong.status_code == 404, wrong.status_code

        ok = client.get("/calendar/test-token.ics")
        assert ok.status_code == 200, ok.status_code
        assert ok.headers["content-type"].startswith("text/calendar"), ok.headers
        assert ok.content.startswith(b"BEGIN:VCALENDAR")

        # Each token serves its own account's calendar, and only that one.
        assert b"X-WR-CALNAME:Momook" in ok.content, ok.content
        marie = client.get("/calendar/marie-token.ics")
        assert marie.status_code == 200, marie.status_code
        assert b"X-WR-CALNAME:Marie" in marie.content, marie.content

        health = client.get("/healthz").json()
        assert health["status"] == "ok", health
        assert [a["account"] for a in health["accounts"]] == ["Momook", "Marie"], health
        assert "marie-token" not in str(health), health

    # An account that has never built serves 503 rather than blocking the
    # request on a fetch, and says so in the health report.
    COLD.add("Marie")

    with TestClient(app_module.app) as client:
        assert client.get("/calendar/test-token.ics").status_code == 200
        cold = client.get("/calendar/marie-token.ics")
        assert cold.status_code == 503, cold.status_code

        health = client.get("/healthz").json()
        assert health["status"] == "degraded", health

    print("ok — per-account tokens, feed, cold start and health endpoints behave")


if __name__ == "__main__":
    main()
