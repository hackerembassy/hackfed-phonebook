import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import urlopen

from app import Config, ExtensionCache, FreePBXClient, UpstreamError, create_server, normalize_extensions


def graphql_payload(records, status=True):
    return {"data": {"fetchAllExtensions": {"status": status, "extension": records}}}


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
        test_case = self

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
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
            "FREEPBX_API_KEY_HEADER": "X-API-Key",
            "FREEPBX_API_METHOD": "DELETE",
            "FREEPBX_API_BODY": "not JSON",
            "FREEPBX_EXTENSIONS_PATH": "result.phones",
            "HOST": "127.0.0.1",
        }, clear=True):
            config = Config.from_env()
        for field in ("api_method", "api_body", "extensions_path", "api_key_header"):
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
        self.assertEqual(self.received_authorization, "Bearer test-access-token")
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