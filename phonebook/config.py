from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .errors import ConfigurationError


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
    log_level: str = "INFO"

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
            cache_ttl_seconds=_positive_int("CACHE_TTL_SECONDS", cls.cache_ttl_seconds),
            request_timeout_seconds=_positive_int("REQUEST_TIMEOUT_SECONDS", cls.request_timeout_seconds),
            host=os.getenv("HOST", cls.host).strip(),
            port=_positive_int("PORT", cls.port),
            tls_verify=json.loads(os.getenv("FREEPBX_TLS_VERIFY", json.dumps(cls.tls_verify)).strip().lower()),
            log_level=os.getenv("LOG_LEVEL", cls.log_level).upper(),
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