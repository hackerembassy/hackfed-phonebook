import json
import io
import os
import ssl
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import urlopen

from app import ConfigurationError, Config, ExtensionCache, FreePBXClient, UpstreamError, create_server, normalize_extensions


def graphql_payload(records, status=True):
    return {"data": {"fetchAllExtensions": {"status": status, "extension": records}}}


class TLSConfigurationTests(unittest.TestCase):
    def config_from_env(self, **overrides):
        with patch.dict(os.environ, {
            "FREEPBX_API_URL": "https://pbx.example.test/admin/api/api/gql",
            "FREEPBX_CLIENT_ID": "test-client",
            "FREEPBX_CLIENT_SECRET": "test-secret",
            **overrides,
        }, clear=True):
            return Config.from_env()

    def test_verification_enabled_by_default(self):
        self.assertTrue(self.config_from_env().tls_verify)

    def test_boolean_values(self):
        for raw in ("true", " TRUE "):
            with self.subTest(raw=raw):
                self.assertIs(self.config_from_env(FREEPBX_TLS_VERIFY=raw).tls_verify, True)
        for raw in ("false", " FALSE "):
            with self.subTest(raw=raw):
                self.assertIs(self.config_from_env(FREEPBX_TLS_VERIFY=raw).tls_verify, False)

    def test_client_passes_scoped_tls_context(self):
        for verify in (True, False):
            with self.subTest(verify=verify), patch("app.build_opener") as build:
                open_url = build.return_value.open
                config = self.config_from_env(FREEPBX_TLS_VERIFY=str(verify))
                client = FreePBXClient(config)
                response = MagicMock()
                response.read.side_effect = [
                    json.dumps({"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600}).encode(),
                    json.dumps(graphql_payload([])).encode(),
                ]
                open_url.return_value.__enter__.return_value = response
                self.assertEqual(client.fetch_extensions(), [])
                context = build.call_args.args[0]._context
                self.assertEqual(context.check_hostname, verify)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED if verify else ssl.CERT_NONE)
                self.assertEqual(open_url.call_args.kwargs["timeout"], config.request_timeout_seconds)
                request = open_url.call_args.args[0]
                self.assertEqual(request.full_url, config.api_url)
                self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
                self.assertEqual(request.method, "POST")

    def test_disabled_verification_logs_warning(self):
        with self.assertLogs("phonebook", level="WARNING") as logs:
            FreePBXClient(self.config_from_env(FREEPBX_TLS_VERIFY="false"))
        self.assertIn("verification is disabled", logs.output[0])
        self.assertNotIn("test-token", logs.output[0])


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(
            api_url="https://pbx.example.test/admin/api/api/gql",
            client_id="test-client", client_secret="test-secret&+=",
        )

    def response(self, payload):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return response

    def token(self, **overrides):
        return {"access_token": "test-access-token", "token_type": "Bearer", "expires_in": 100, **overrides}

    def test_environment_configuration(self):
        env = {
            "FREEPBX_API_URL": self.config.api_url,
            "FREEPBX_CLIENT_ID": "test-client",
            "FREEPBX_CLIENT_SECRET": "test-secret",
            "FREEPBX_TOKEN_URL": "https://pbx.example.test/oauth/token",
            "FREEPBX_SCOPE": "gql:core:read",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.client_id, "test-client")
        self.assertEqual(config.client_secret, "test-secret")
        self.assertEqual(config.token_url, env["FREEPBX_TOKEN_URL"])
        self.assertEqual(config.scope, "gql:core:read")
        self.assertNotIn("test-secret", repr(config))
        for missing in ("FREEPBX_CLIENT_ID", "FREEPBX_CLIENT_SECRET"):
            incomplete = {k: v for k, v in env.items() if k != missing}
            incomplete["FREEPBX_API_KEY"] = "Bearer legacy-token"
            with self.subTest(missing=missing), patch.dict(os.environ, incomplete, clear=True):
                with self.assertRaises(ConfigurationError):
                    Config.from_env()
        for legacy_key in ("", "Bearer legacy-token"):
            with self.subTest(legacy_key=legacy_key), patch.dict(os.environ, {
                "FREEPBX_API_URL": self.config.api_url,
                "FREEPBX_API_KEY": legacy_key,
            }, clear=True):
                with self.assertRaisesRegex(ConfigurationError, "FREEPBX_CLIENT_ID and FREEPBX_CLIENT_SECRET"):
                    Config.from_env()

    def test_token_request_reuse_and_early_renewal(self):
        client = FreePBXClient(self.config)
        with patch.object(client._opener, "open") as open_url, patch("app.time.monotonic", return_value=100) as clock:
            open_url.side_effect = [
                self.response(self.token()), self.response(graphql_payload([])),
                self.response(graphql_payload([])),
                self.response(self.token(access_token="replacement")), self.response(graphql_payload([])),
            ]
            client.fetch_extensions()
            clock.return_value = 189
            client.fetch_extensions()
            self.assertEqual(open_url.call_count, 3)
            clock.return_value = 190
            client.fetch_extensions()
            calls = open_url.call_args_list
            token_request = calls[0].args[0]
            self.assertEqual(token_request.full_url, "https://pbx.example.test/admin/api/api/token")
            self.assertEqual(token_request.method, "POST")
            self.assertEqual(token_request.get_header("Content-type"), "application/x-www-form-urlencoded")
            self.assertIsNone(token_request.get_header("Authorization"))
            self.assertEqual(parse_qs(token_request.data.decode()), {
                "grant_type": ["client_credentials"], "client_id": ["test-client"],
                "client_secret": ["test-secret&+="], "scope": ["gql:core:read"],
            })
            self.assertEqual(calls[1].args[0].get_header("Authorization"), "Bearer test-access-token")
            self.assertEqual(calls[4].args[0].get_header("Authorization"), "Bearer replacement")
            self.assertEqual(calls[0].kwargs["timeout"], self.config.request_timeout_seconds)

    def test_explicit_token_url(self):
        config = Config(**{**vars(self.config), "token_url": "https://pbx.example.test/oauth/token"})
        client = FreePBXClient(config)
        with patch.object(client._opener, "open", return_value=self.response(self.token())) as open_url:
            self.assertEqual(client._authorization(), "Bearer test-access-token")
            self.assertEqual(open_url.call_args.args[0].full_url, config.token_url)

    def test_invalid_token_responses(self):
        payloads = [None, [], {}, {"error": "invalid_client"}]
        payloads += [self.token(expires_in=value) for value in (None, True, 0, -1, "bad", "nan", "inf")]
        payloads += [self.token(access_token=value) for value in (None, "", "bad\r\ntoken", 123)]
        payloads += [self.token(token_type="Basic")]
        for payload in payloads:
            with self.subTest(payload=payload):
                client = FreePBXClient(self.config)
                with patch.object(client._opener, "open", return_value=self.response(payload)):
                    with self.assertRaisesRegex(UpstreamError, "invalid token response"):
                        client.fetch_extensions()
                    self.assertEqual(client._access_token, "")

    def test_token_failures_are_sanitized(self):
        for error in (
            HTTPError(self.config.api_url, 401, "test-secret", {}, io.BytesIO(b'test-secret')),
            URLError("test-secret"), TimeoutError("test-secret"),
        ):
            client = FreePBXClient(self.config)
            with self.subTest(error=type(error)), patch.object(client._opener, "open", side_effect=error):
                with self.assertRaises(UpstreamError) as caught:
                    client.fetch_extensions()
                self.assertIn("token endpoint", str(caught.exception))
                self.assertNotIn("test-secret", str(caught.exception))
        client = FreePBXClient(self.config)
        response = self.response({})
        response.__enter__.return_value.read.return_value = b'not JSON test-secret'
        with patch.object(client._opener, "open", return_value=response):
            with self.assertRaisesRegex(UpstreamError, "token endpoint returned invalid JSON"):
                client.fetch_extensions()

    def test_unauthorized_api_invalidates_token(self):
        client = FreePBXClient(self.config)
        with patch.object(client._opener, "open") as open_url:
            open_url.side_effect = [
                self.response(self.token()),
                HTTPError(self.config.api_url, 401, "Unauthorized", {}, io.BytesIO()),
                self.response(self.token(access_token="replacement")), self.response(graphql_payload([])),
            ]
            with self.assertRaisesRegex(UpstreamError, "API returned HTTP 401"):
                client.fetch_extensions()
            self.assertEqual(client._token_expires_at, 0)
            self.assertEqual(client.fetch_extensions(), [])
            self.assertEqual(open_url.call_count, 4)


class NormalizeExtensionsTests(unittest.TestCase):
    def test_normalizes_nested_records_and_sorts_numerically(self):
        payload = graphql_payload([
            {"extensionId": "20", "user": {"name": "Twenty"}},
            {"extensionId": "3", "user": {"name": "Three"}},
            {"extensionId": "100", "user": {"name": "One Hundred"}},
        ])

        self.assertEqual(
            normalize_extensions(payload),
            [
                {"extension": "3", "name": "Three"},
                {"extension": "20", "name": "Twenty"},
                {"extension": "100", "name": "One Hundred"},
            ],
        )

    def test_rejects_other_response_paths(self):
        payload = {"result": {"phones": [{"number": 101, "name": "Alice"}]}}

        with self.assertRaisesRegex(UpstreamError, "expected path"):
            normalize_extensions(payload)

    def test_accepts_empty_extension_list(self):
        self.assertEqual(normalize_extensions(graphql_payload([])), [])

    def test_falls_back_to_extension_when_user_is_null(self):
        self.assertEqual(
            normalize_extensions(graphql_payload([{"extensionId": "101", "user": None}])),
            [{"extension": "101", "name": "101"}],
        )

    def test_rejects_graphql_errors_and_invalid_responses(self):
        for payload in (
            {"errors": [{"message": "Not authorized"}]},
            dict(graphql_payload([]), errors=[{"message": "Partial failure"}]),
            graphql_payload([], status=False),
            graphql_payload(None),
            graphql_payload({"items": []}),
            graphql_payload([None]),
            {"data": None},
        ):
            with self.subTest(payload=payload), self.assertRaises(UpstreamError):
                normalize_extensions(payload)


class ExtensionCacheTests(unittest.TestCase):
    def test_serves_last_snapshot_when_refresh_fails(self):
        calls = 0

        def fetch():
            nonlocal calls
            calls += 1
            if calls == 1:
                return [{"extension": "100", "name": "Alice"}]
            raise UpstreamError("temporary failure")

        cache = ExtensionCache(fetch, ttl_seconds=60)
        first = cache.get()
        cache._expires_at = 0
        stale = cache.get()

        self.assertFalse(first["stale"])
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["extensions"], first["extensions"])
        self.assertEqual(cache.status()["status"], "degraded")


class ApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.received_api_key = None
        self.received_authorization = None
        self.received_body = None
        self.received_content_type = None
        self.token_requests = []
        self.redirect_token = False
        test_case = self

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path == "/admin/api/api/token":
                    test_case.token_requests.append(parse_qs(
                        self.rfile.read(int(self.headers["Content-Length"])).decode()
                    ))
                    if test_case.redirect_token:
                        self.send_response(302)
                        self.send_header("Location", "/unexpected-redirect")
                        self.end_headers()
                        return
                    body = json.dumps({
                        "access_token": "integration-token", "token_type": "Bearer", "expires_in": 3600,
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                test_case.received_api_key = self.headers.get("X-API-Key")
                test_case.received_authorization = self.headers.get("Authorization")
                test_case.received_content_type = self.headers.get("Content-Type")
                test_case.received_body = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                body = json.dumps(
                    graphql_payload([{"extensionId": "101", "user": {"name": "Bob"}}])
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever)
        self.upstream_thread.start()

        with patch.dict(os.environ, {
            "FREEPBX_API_URL": f"http://127.0.0.1:{self.upstream.server_port}/admin/api/api/gql",
            "FREEPBX_API_KEY": "Bearer test-access-token",
            "FREEPBX_CLIENT_ID": "integration-client",
            "FREEPBX_CLIENT_SECRET": "integration-secret",
            "FREEPBX_API_KEY_HEADER": "X-API-Key",
            "FREEPBX_API_METHOD": "DELETE",
            "FREEPBX_API_BODY": "not JSON",
            "FREEPBX_EXTENSIONS_PATH": "result.phones",
            "HOST": "127.0.0.1",
        }, clear=True):
            config = Config.from_env()
        for field in ("api_key", "api_method", "api_body", "extensions_path", "api_key_header"):
            self.assertFalse(hasattr(config, field))
        # A zero port selects an ephemeral port for the test server.
        config = Config(**{**vars(config), "port": 0})
        self.service = create_server(config, FreePBXClient(config))
        self.service_thread = threading.Thread(target=self.service.serve_forever)
        self.service_thread.start()

    def tearDown(self):
        self.service.shutdown()
        self.service.server_close()
        self.service_thread.join()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join()

    def test_fixed_graphql_post_ignores_legacy_environment_overrides(self):
        with urlopen(
            f"http://127.0.0.1:{self.service.server_port}/extensions"
        ) as response:
            payload = json.load(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(self.received_authorization, "Bearer integration-token")
        self.assertEqual(len(self.token_requests), 1)
        self.assertIsNone(self.received_api_key)
        self.assertEqual(self.received_content_type, "application/json")
        self.assertEqual(set(self.received_body), {"query"})
        self.assertEqual(
            " ".join(self.received_body["query"].split()),
            "query { fetchAllExtensions { status message extension { extensionId user { name } } } }",
        )
        self.assertEqual(
            payload["extensions"], [{"extension": "101", "name": "Bob"}]
        )
        self.assertFalse(payload["stale"])
        self.assertIsNotNone(payload["updated_at"])

    def test_oauth_flow_over_http(self):
        client = FreePBXClient(Config(
            api_url=f"http://127.0.0.1:{self.upstream.server_port}/admin/api/api/gql",
            client_id="integration-client", client_secret="integration-secret&+",
        ))
        for _ in range(2):
            self.assertEqual(client.fetch_extensions(), [{"extension": "101", "name": "Bob"}])
        self.assertEqual(len(self.token_requests), 1)
        self.assertEqual(self.token_requests[0]["client_secret"], ["integration-secret&+"])
        self.assertEqual(self.received_authorization, "Bearer integration-token")

    def test_token_redirect_is_rejected(self):
        self.redirect_token = True
        client = FreePBXClient(Config(
            api_url=f"http://127.0.0.1:{self.upstream.server_port}/admin/api/api/gql",
            client_id="integration-client", client_secret="integration-secret",
        ))
        with self.assertRaisesRegex(UpstreamError, "token endpoint returned HTTP 302"):
            client.fetch_extensions()

    def test_health_endpoint_does_not_fetch_upstream(self):
        with urlopen(f"http://127.0.0.1:{self.service.server_port}/health") as response:
            payload = json.load(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["has_snapshot"])
        self.assertIsNone(self.received_authorization)
        self.assertIsNone(self.received_api_key)


if __name__ == "__main__":
    unittest.main()