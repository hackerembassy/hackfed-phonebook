from __future__ import annotations

import json
import logging
import math
import os
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


LOGGER = logging.getLogger("phonebook")
EXTENSIONS_QUERY = """
query {
    fetchAllExtensions {
        status
        message
        extension {
            extensionId
            user { name }
        }
    }
}
"""
EXTENSIONS_PATH = "data.fetchAllExtensions.extension"
EXTENSION_KEYS = ("extension", "extensionId", "extension_id", "number", "user_extension")
NAME_KEYS = ("name", "display_name", "displayname", "description")


class ConfigurationError(ValueError):
    pass


class UpstreamError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    api_url: str
    client_id: str
    client_secret: str = field(repr=False)
    cache_ttl_seconds: int = 60
    request_timeout_seconds: int = 10
    host: str = "0.0.0.0"
    port: int = 8080
    tls_verify: bool = True
    token_url: str = ""
    scope: str = "gql:core:read"

    @classmethod
    def from_env(cls) -> "Config":
        api_url = os.getenv("FREEPBX_API_URL", "").strip()
        client_id = os.getenv("FREEPBX_CLIENT_ID", "").strip()
        client_secret = os.getenv("FREEPBX_CLIENT_SECRET", "").strip()
        if not api_url:
            raise ConfigurationError("FREEPBX_API_URL is required")
        if not client_id or not client_secret:
            raise ConfigurationError("FREEPBX_CLIENT_ID and FREEPBX_CLIENT_SECRET are both required")

        return cls(
            api_url=api_url,
            client_id=client_id,
            client_secret=client_secret,
            token_url=os.getenv("FREEPBX_TOKEN_URL", "").strip(),
            scope=os.getenv("FREEPBX_SCOPE", "gql:core:read").strip(),
            cache_ttl_seconds=_positive_int("CACHE_TTL_SECONDS", 60),
            request_timeout_seconds=_positive_int("REQUEST_TIMEOUT_SECONDS", 10),
            host=os.getenv("HOST", "0.0.0.0").strip(),
            port=_positive_int("PORT", 8080),
            tls_verify=json.loads(os.getenv("FREEPBX_TLS_VERIFY", "true").strip().lower()),
        )


def _positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _select_path(payload: Any, path: str) -> Any:
    selected = payload
    for part in path.split("."):
        if not isinstance(selected, dict) or part not in selected:
            raise UpstreamError(f"response does not contain expected path: {path}")
        selected = selected[part]
    return selected


def _looks_like_extension_list(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    if not value:
        return True
    return all(
        isinstance(item, dict) and any(key in item for key in EXTENSION_KEYS)
        for item in value
    )


def _first_text(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_extensions(payload: Any) -> list[dict[str, str]]:
    if isinstance(payload, dict) and payload.get("errors"):
        raise UpstreamError("FreePBX GraphQL returned errors")
    records = _select_path(payload, EXTENSIONS_PATH)
    if payload["data"]["fetchAllExtensions"].get("status") is not True:
        raise UpstreamError("FreePBX could not fetch extensions")
    if not _looks_like_extension_list(records):
        raise UpstreamError(f"expected an extension list at {EXTENSIONS_PATH}")

    extensions: dict[str, dict[str, str]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        extension = _first_text(record, EXTENSION_KEYS)
        if not extension:
            continue
        name = _first_text(record, NAME_KEYS)
        if not name and isinstance(record.get("user"), dict):
            name = _first_text(record["user"], NAME_KEYS)
        if not name:
            name = extension
        extensions[extension] = {"extension": extension, "name": name}

    return sorted(
        extensions.values(),
        key=lambda item: (
            0 if item["extension"].isdigit() else 1,
            int(item["extension"]) if item["extension"].isdigit() else item["extension"].casefold(),
        ),
    )


class _NoRedirect(HTTPRedirectHandler):
    # Never forward credentials to a redirect destination.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FreePBXClient:
    def __init__(self, config: Config):
        self.config = config
        self._token_lock = threading.Lock()
        self._access_token = ""
        self._token_expires_at = 0.0
        self._ssl_context = ssl.create_default_context()
        if not config.tls_verify:
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE
            LOGGER.warning(
                "FreePBX TLS certificate verification is disabled; "
                "the upstream server identity will not be verified"
            )
        self._opener = build_opener(HTTPSHandler(context=self._ssl_context), _NoRedirect())

    def _authorization(self) -> str:
        with self._token_lock:
            now = time.monotonic()
            if self._access_token and now < self._token_expires_at:
                return "Bearer " + self._access_token

            parts = urlsplit(self.config.api_url)
            token_url = self.config.token_url or urlunsplit(
                (parts.scheme, parts.netloc, parts.path.rsplit("/", 1)[0] + "/token", "", "")
            )
            request = Request(
                token_url,
                data=urlencode({
                    "grant_type": "client_credentials",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "scope": self.config.scope,
                }).encode("utf-8"),
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            payload = self._request_json(request, "token endpoint")
            if not isinstance(payload, dict):
                raise UpstreamError("FreePBX token endpoint returned an invalid token response")
            token = payload.get("access_token")
            token_type = payload.get("token_type")
            expires_in = payload.get("expires_in")
            try:
                lifetime = float(expires_in)
            except (TypeError, ValueError, OverflowError):
                lifetime = 0.0
            if (
                not isinstance(token, str) or not token or any(c.isspace() for c in token)
                or not token.isascii() or not token.isprintable()
                or not isinstance(token_type, str) or token_type.lower() != "bearer"
                or isinstance(expires_in, bool) or not math.isfinite(lifetime) or lifetime <= 0
            ):
                raise UpstreamError("FreePBX token endpoint returned an invalid token response")
            self._access_token = token
            # Start expiry at request time, with a margin even for short-lived tokens.
            self._token_expires_at = now + lifetime - min(30.0, lifetime * 0.1)
            return "Bearer " + token

    def fetch_extensions(self) -> list[dict[str, str]]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self._authorization(),
        }
        body = json.dumps({"query": EXTENSIONS_QUERY}).encode("utf-8")

        request = Request(
            self.config.api_url,
            data=body,
            headers=headers,
            method="POST",
        )
        return normalize_extensions(self._request_json(request, "API"))

    def _request_json(self, request: Request, endpoint: str) -> Any:
        try:
            with self._opener.open(
                request,
                timeout=self.config.request_timeout_seconds,
            ) as response:
                return json.load(response)
        except HTTPError as error:
            # Error bodies can echo secrets; never expose them in logs or public responses.
            error.close()
            if endpoint == "API" and error.code == 401:
                with self._token_lock:
                    self._token_expires_at = 0.0
            raise UpstreamError(f"FreePBX {endpoint} returned HTTP {error.code}") from None
        except (URLError, OSError):
            raise UpstreamError(f"could not reach FreePBX {endpoint}") from None
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise UpstreamError(f"FreePBX {endpoint} returned invalid JSON") from None


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


def make_handler(cache: ExtensionCache) -> type[BaseHTTPRequestHandler]:
    class PhonebookHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/extensions":
                try:
                    self._send_json(200, cache.get())
                except (ConfigurationError, UpstreamError) as error:
                    LOGGER.error("Unable to load phonebook: %s", error)
                    self._send_json(502, {"error": str(error)})
            elif self.path == "/health":
                self._send_json(200, cache.status())
            else:
                self._send_json(404, {"error": "not found"})

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
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


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = Config.from_env()
    except ConfigurationError as error:
        raise SystemExit(f"Configuration error: {error}") from error

    server = create_server(config)
    LOGGER.info("Serving phonebook on http://%s:%s", config.host, config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()