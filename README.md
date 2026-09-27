# FreePBX JSON Phonebook

A minimal, read-only HTTP service that retrieves extensions from a FreePBX 17 API endpoint and exposes them as normalized JSON. It runs with Podman, has no database, and has no Python dependencies outside the standard library.

## Configure

Copy the environment template:

```bash
cp .env.example .env
```

Set these required values in `.env`:

- `FREEPBX_API_URL`: the GraphQL endpoint, normally `https://<pbx>/admin/api/api/gql`.
- `FREEPBX_CLIENT_ID`: the Machine-to-Machine application's client ID.
- `FREEPBX_CLIENT_SECRET`: its client secret.

Allow `gql:core:read` in the application's scopes. `FREEPBX_SCOPE` defaults to
that scope. `FREEPBX_TOKEN_URL` optionally overrides the token endpoint; by
default the final path segment of the GraphQL URL is replaced with `token`.
Use the Token URL shown by your FreePBX API application if it differs.

The service posts form-encoded `grant_type=client_credentials`, `client_id`,
`client_secret`, and `scope` to the token endpoint on the first extension fetch.
It keeps the access token in memory and obtains a new one on demand before
expiry (using `expires_in`). It does not require or store a refresh token.
Token requests use the same TLS verification and timeout settings as GraphQL.
Redirects are rejected to avoid forwarding credentials to another endpoint.

Client-credentials OAuth is the only authentication mode. Both client ID and
client secret are required; manually supplied access tokens are not supported.

Restart the debug session or recreate the container after changing `.env`.
The app reads process environment variables; it does not load `.env` itself.
Use the debugger's environment-file support or Compose's `env_file` setting.

`FREEPBX_TLS_VERIFY` defaults to `true`. Set it to `false` to connect to a
self-signed HTTPS server without certificate or hostname verification. HTTPS
traffic remains encrypted, but the server's identity is not verified, allowing
server impersonation. Prefer a trusted certificate for production. This setting
applies only to the FreePBX client's requests and logs a warning when disabled.
It does not fix upstream HTTP 500 errors. Recreate the container after changing
environment variables for the setting to take effect.

The request is fixed: HTTP `POST`, a JSON body containing the query `query { fetchAllExtensions { status message extension { extensionId user { name } } } }`, and the response path `data.fetchAllExtensions.extension`. Method, body, and response path are not configurable; legacy environment overrides are ignored.

The authentication header is hardcoded to `Authorization`; its name is not configurable and legacy header-name overrides are ignored. OAuth tokens are sent with the `Bearer ` prefix automatically. Token endpoint failures return a sanitized error identifying the endpoint without exposing credentials or upstream response bodies. An API HTTP 401 invalidates the cached token for the next fetch; other HTTP errors are not automatically retried.

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

## Container image

The `Publish Docker image` GitHub Actions workflow builds the Dockerfile and
pushes to `ghcr.io/hackerembassy/hackfed-phonebook` on pushes to `main` or a
manual run from the Actions tab.

- Every build publishes an image tag using the GitHub Actions run ID
  (`github.run_id`). Re-running the same workflow run reuses that tag.
- Builds from `main` also publish `latest`.

Publishing uses the built-in `GITHUB_TOKEN` with `packages: write`; no additional
registry secrets are required. GHCR packages are initially private by default.
Change the package visibility to public in GitHub's package settings if anonymous
pulls are needed, or authenticate to GHCR before pulling a private image.

## Test

```bash
python -m unittest discover -s tests -v
```

## Security

The service only reads FreePBX data. Give its API application the smallest available read-only permissions, keep `.env` out of version control, and expose port 8080 only to trusted networks. Add a reverse proxy with authentication before making the endpoint publicly reachable.