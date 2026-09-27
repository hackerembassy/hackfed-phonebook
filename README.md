# FreePBX JSON Phonebook

A minimal, read-only HTTP service that retrieves extensions from a FreePBX 17 API endpoint and exposes them as normalized JSON. It runs with Podman, has no database, and has no Python dependencies outside the standard library.

## Configure

Copy the environment template:

```bash
cp .env.example .env
```

Set these required values in `.env`:

- `FREEPBX_API_URL`: the GraphQL endpoint, normally `https://<pbx>/admin/api/api/gql`.
- `FREEPBX_API_KEY`: the full bearer authorization value, `"Bearer <access-token>"`.

`FREEPBX_TLS_VERIFY` defaults to `true`. Set it to `false` to connect to a
self-signed HTTPS server without certificate or hostname verification. HTTPS
traffic remains encrypted, but the server's identity is not verified, allowing
server impersonation. Prefer a trusted certificate for production. This setting
applies only to the FreePBX client's requests and logs a warning when disabled.
It does not fix upstream HTTP 500 errors. Recreate the container after changing
environment variables for the setting to take effect.

The request is fixed: HTTP `POST`, a JSON body containing the query `query { fetchAllExtensions { status message extension { extensionId user { name } } } }`, and the response path `data.fetchAllExtensions.extension`. Method, body, and response path are not configurable; legacy environment overrides are ignored.

The authentication header is hardcoded to `Authorization`; its name is not configurable and legacy header-name overrides are ignored. The service sends `FREEPBX_API_KEY` verbatim as its value, so include the `Bearer ` prefix. Token acquisition and renewal are not implemented; obtain the token separately using a Machine-to-Machine application with the `gql:core:read` scope.

Each source record must include one of `extension`, `extensionId`, `extension_id`, `number`, or `user_extension`. Names are read from `name`, `display_name`, `displayname`, or `description`, including those fields inside a nested `user` object.

## Run

```bash
podman-compose up --build -d
curl http://localhost:8080/extensions
```

Stop it with:

```bash
podman-compose down
```

Example response:

```json
{
  "updated_at": "2026-09-15T12:00:00Z",
  "stale": false,
  "extensions": [
    {"extension": "100", "name": "Alice Smith"},
    {"extension": "101", "name": "Bob Jones"}
  ]
}
```

`GET /extensions` refreshes from FreePBX after `CACHE_TTL_SECONDS`, which defaults to 60 seconds. If a refresh fails after at least one successful request, the last snapshot is returned with `"stale": true`. Before the first successful fetch, an upstream failure returns HTTP 502.

`GET /health` reports whether a snapshot exists and whether the last refresh failed. It does not call FreePBX.

## Test

```bash
python -m unittest discover -s tests -v
```

## Security

The service only reads FreePBX data. Give its API application the smallest available read-only permissions, keep `.env` out of version control, and expose port 8080 only to trusted networks. Add a reverse proxy with authentication before making the endpoint publicly reachable.