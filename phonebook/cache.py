from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .errors import ConfigurationError, UpstreamError


LOGGER = logging.getLogger("phonebook")


class ExtensionCache:
    def __init__(self, fetch: Callable[[], list[dict[str, str]]], ttl_seconds: int):
        self.fetch = fetch
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._extensions: list[dict[str, str]] | None = None
        self._updated_at: str | None = None
        self._expires_at = 0.0
        self._last_error: str | None = None

    def get(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._extensions is not None and now < self._expires_at:
                return self._snapshot(stale=False)

            try:
                self._extensions = self.fetch()
                self._updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                self._expires_at = now + self.ttl_seconds
                self._last_error = None
                return self._snapshot(stale=False)
            except (ConfigurationError, UpstreamError) as error:
                self._last_error = str(error)
                if self._extensions is None:
                    raise
                self._expires_at = now + self.ttl_seconds
                LOGGER.warning("Serving stale phonebook: %s", error)
                return self._snapshot(stale=True)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "degraded" if self._last_error else "ok",
                "has_snapshot": self._extensions is not None,
                "last_error": self._last_error,
            }

    def _snapshot(self, stale: bool) -> dict[str, Any]:
        return {
            "updated_at": self._updated_at,
            "stale": stale,
            "extensions": self._extensions,
        }