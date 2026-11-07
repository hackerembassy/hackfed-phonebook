from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .cache import ExtensionCache
from .config import Config
from .errors import ConfigurationError, UpstreamError
from .freepbx import FreePBXClient
from .renderers import render_c60_phonebook, render_yealink_phonebook


LOGGER = logging.getLogger("phonebook")


def make_handler(cache: ExtensionCache) -> type[BaseHTTPRequestHandler]:
    xml_renderers = {
        "/yealink.xml": render_yealink_phonebook,
        "/poly/000000000000-directory.xml": render_c60_phonebook,
    }

    class PhonebookHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/extensions" or self.path in xml_renderers:
                renderer = xml_renderers.get(self.path)
                try:
                    snapshot = cache.get()
                    if renderer is not None:
                        self._send_body(200, renderer(snapshot["extensions"]), "text/xml; charset=utf-8")
                    else:
                        self._send_json(200, snapshot)
                except (ConfigurationError, UpstreamError) as error:
                    LOGGER.error("Unable to load phonebook: %s", error)
                    if renderer is not None:
                        # Signal failure rather than replace contacts with a fake error contact.
                        self._send_body(502, renderer([]), "text/xml; charset=utf-8")
                    else:
                        self._send_json(502, {"error": str(error)})
            elif self.path == "/health":
                self._send_json(200, cache.status())
            else:
                self._send_json(404, {"error": "not found"})

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self._send_body(status, body, "application/json; charset=utf-8")

        def _send_body(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.info("%s - %s", self.address_string(), format % args)

    return PhonebookHandler


def create_server(config: Config, client: FreePBXClient | None = None) -> ThreadingHTTPServer:
    api_client = client or FreePBXClient(config)
    cache = ExtensionCache(api_client.fetch_extensions, config.cache_ttl_seconds)
    return ThreadingHTTPServer((config.host, config.port), make_handler(cache))