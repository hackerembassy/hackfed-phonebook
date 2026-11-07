# FreePBX Phonebook

A minimal, read-only HTTP service that retrieves extensions from a FreePBX 17 API endpoint and exposes them as normalized JSON, a Yealink XML remote phonebook, and a Poly Trio C60 XML directory. It runs with Podman, has no database, and has no Python dependencies outside the standard library.

## Configure

Copy the environment template:

```bash
cp .env.example .env
```

Set these required values in `.env`:

- `FREEPBX_API_URL`: the GraphQL endpoint, normally `https://<pbx>/admin/api/api/gql`.
- `FREEPBX_CLIENT_ID`: the Machine-to-Machine application's client ID.
- `FREEPBX_CLIENT_SECRET`: its client secret.

Allow `gql:core:read` in the application's scopes.

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

## Yealink W52 remote phonebook

`GET /yealink.xml` returns a flat `YealinkIPPhoneDirectory` with one
`DirectoryEntry` per extension, containing `Name` and `Telephone`. It uses
`text/xml; charset=utf-8`, includes an XML declaration, and has no UTF-8 BOM.
Names are XML-escaped and limited to 99 characters; invalid XML 1.0 characters
are removed. Dial strings remain strings, preserving leading zeros and SIP URLs.

The XML and JSON endpoints share the same FreePBX client and cache. No separate
request to the public `/extensions` endpoint is needed. On a refresh failure,
the XML endpoint returns the last successful directory with HTTP 200. If no
snapshot exists yet, it returns HTTP 502 with an empty XML directory, not a
fake dialable error contact.

After deploying this version and routing `/yealink.xml` through your reverse
proxy, configure the **W52 base station's web interface**:

1. Open **Directory > Remote Phone Book**.
2. Set **Phone Book URL** to `https://phonebook.hackem.cc/yealink.xml`.
3. Set **Phone Book Name** to `Extensions` and save.
4. On the handset, open **Menu > Directory > Remote Phone Book** and select
  **Extensions**. Menu labels can vary by firmware.

For auto-provisioning, set `remote_phonebook.data.1.url` to that URL and
`remote_phonebook.data.1.name` to `Extensions`.

The base station must be able to reach the URL directly without a browser login
or redirects. If older W52 firmware cannot negotiate your HTTPS/TLS setup, use
an HTTP URL on a trusted LAN or update the firmware; do not expose an unsecured
phonebook publicly. The phone caches its downloaded directory independently of
this service, so use its manual update or scheduled refresh to see changes.

## Poly Trio C60 contact directory

`GET /poly/000000000000-directory.xml` returns a Poly UC
Software directory: `directory > item_list > item`. Each item contains `fn`
(first name), `ln` (remaining name), `ct` (dial string), and `lb` (full-name label).
Names and labels are limited to 40 UTF-8 bytes without splitting characters;
invalid XML characters in names are removed and XML syntax is escaped.
Empty names fall back to the dial string. Optional speed-dial, ringtone,
presence, diversion, and line-binding fields are omitted to retain phone defaults.

The first 500 eligible contacts in the existing extension order are included,
within a 4 MiB serialized XML limit. Blank dial strings, those over 128 characters,
those containing invalid XML characters, and duplicates are skipped (first wins).
Dial strings are not truncated or repaired; leading zeros and SIP URIs are
preserved. Omitted-contact counts are logged. JSON and Yealink output remain
unrestricted by these C60 limits.

The endpoint shares the JSON/Yealink cache and returns `text/xml; charset=utf-8`, an
XML declaration, and no BOM. Refresh failures serve the last snapshot with
HTTP 200; initial failures return HTTP 502 with an empty Poly directory.

For provisioning:

1. Route `000000000000-directory.xml` on the phone's existing provisioning
  server to this service's `/poly/000000000000-directory.xml` endpoint,
  without redirects or browser logins. For example:
  `https://phonebook.hackem.cc/poly/000000000000-directory.xml`.
  If provisioning directly from this namespace, use
  `https://phonebook.hackem.cc/poly/` as the provisioning base URL so the phone
  appends the required filename. Other configuration/firmware files must also
  be routed there by your provisioning server; this service does not supply them.
  The root `/000000000000-directory.xml` is not served, leaving that namespace
  available for other vendors/providers.
2. In the phone's provisioned configuration, set `feature.directory.enabled="1"`
  and `dir.local.readonly="1"`. Local directory support may be disabled in
  Teams/Skype base profiles; check the phone's firmware/profile support.
3. Reboot or trigger a directory provisioning sync. Where supported, set
  `voIpProt.SIP.specialEvent.checkSync.downloadDirectory="1"` for check-sync
  directory updates. The phone's download cycle is separate from the service TTL.

Device-specific `<mac>-directory.xml` requests intentionally return 404: this
service supplies only the global directory and does not accept contact uploads
or serve other provisioning files. Keep existing device provisioning on its
current server; remove conflicting personal contacts there if they override
global entries. Use trusted network access and a certificate the phone accepts.

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

### Host the published image

Use `docker compose -f compose.hosting.yml up -d` to deploy the published
`ghcr.io/hackerembassy/hackfed-phonebook:latest` image without building locally.
The hosting file is standalone; do not combine it with `compose.yml`.

Configuration is interpolated from the shell environment or Compose's `.env`
file, with shell values taking precedence. Set `FREEPBX_API_URL`,
`FREEPBX_CLIENT_ID`, and `FREEPBX_CLIENT_SECRET`; Compose rejects missing or
empty values. Optional application settings use the defaults described above.

The published port defaults to `127.0.0.1:8080`. Set `BIND_ADDRESS` to a trusted
network interface (or `0.0.0.0` for all interfaces) and `PORT` to change the host
port. The service always listens on `0.0.0.0:8080` inside the container.

Running the deployment command again pulls the latest image and recreates the
container if needed. Stop it with `docker compose -f compose.hosting.yml down`.

## Code structure

`app.py` contains only the `main()` entry point and startup/shutdown handling.
Application logic is grouped in the `phonebook` package:

- `config.py`: environment configuration and validation.
- `errors.py`: shared configuration and upstream exceptions.
- `extensions.py`: FreePBX response validation and extension normalization.
- `freepbx.py`: OAuth tokens and GraphQL HTTP requests.
- `cache.py`: thread-safe extension snapshots and stale fallback.
- `renderers.py`: Yealink and Poly XML formats and device limits.
- `server.py`: HTTP routes, responses, and server construction.

The installed `freepbx-phonebook` command and `python app.py` remain unchanged.

## Test

```bash
python -m unittest discover -s tests -v
```

## Security

The service only reads FreePBX data. Give its API application the smallest available read-only permissions, keep `.env` out of version control, and expose port 8080 only to trusted networks. Add a reverse proxy with authentication before making the endpoint publicly reachable.