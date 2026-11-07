from __future__ import annotations

import json
import logging
import math
import ssl
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .config import Config
from .errors import UpstreamError
from .extensions import normalize_extensions


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
            token_url = urlunsplit(
                (parts.scheme, parts.netloc, parts.path.rsplit("/", 1)[0] + "/token", "", "")
            )
            request = Request(
                token_url,
                data=urlencode({
                    "grant_type": "client_credentials",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "scope": "gql:core:read",
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