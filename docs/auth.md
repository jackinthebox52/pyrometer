# Authentication

Pyrometer requires authentication by default. Nothing is readable or writable
without a credential except `GET /health`, the dashboard's static files, and the
`/auth/*` session endpoints.

## Who authenticates how

| Caller | Credential | Access |
|--------|-----------|--------|
| SDK / scripts | API key in `Authorization: Bearer pyro_…` | Per-key scopes: `ingest` (writes), `read` (reads) |
| Dashboard behind an auth proxy (e.g. authentik) | Identity header set by the proxy | Read-only |
| Dashboard without a proxy | Paste a `read` key into the login screen → HttpOnly session cookie | Read-only |

Keys are separate from scopes on purpose: a training script gets an `ingest`-only
key, so if it leaks, nobody can read your runs with it.

## API keys

Manage keys on the machine (or container) running the server:

```bash
pyrometer keys create --name my-project --scopes ingest   # prints the key ONCE
pyrometer keys create --name dashboard --scopes read
pyrometer keys list                                       # metadata only
pyrometer keys revoke <id>
```

Changes take effect immediately; no restart is needed.

Properties:

- 256-bit random secret (`secrets.token_urlsafe(32)`), format `pyro_<id>_<secret>`.
- Only the SHA-256 hash is stored, in `~/.pyrometer/keys.json` (mode `0600`;
  override with `PYROMETER_KEYS_FILE`). The full key is shown once and cannot be
  recovered. A leaked database or keys file does not yield usable keys.
- Verification is constant-time, and an unknown key id costs the same as a wrong secret.
- A presented-but-invalid `Authorization` header is rejected outright; it never
  falls back to another credential.

### Using a key from the SDK

```bash
export PYROMETER_SERVER=http://pyrometer.example.com:8321
export PYROMETER_API_KEY=pyro_...        # from your secret store; never commit it
```

```python
run = pyrometer.init(name="baseline")    # reads both from the environment
```

Or pass `server=` / `api_key=` explicitly. If the server rejects the key,
`init()` raises `PermissionError` with instructions.

## Reverse-proxy identity (authentik)

To put the dashboard behind an SSO proxy, tell Pyrometer which peers to trust and
which header carries the username:

```bash
PYROMETER_TRUSTED_PROXIES=127.0.0.1,172.16.0.0/12   # IPs/CIDRs of your proxy as the server sees it
PYROMETER_PROXY_USER_HEADER=X-authentik-username    # default
```

The header is honoured **only** when the TCP peer is in `PYROMETER_TRUSTED_PROXIES`
(uvicorn's `X-Forwarded-For` rewriting is disabled so the real peer is used).
Your proxy **must** overwrite or clear that header on every request — see
[deployment.md](deployment.md#behind-nginx-and-authentik). Proxy identities are
read-only; writes always need an API key.

## Local mode

```bash
pyrometer serve --no-auth      # or PYROMETER_AUTH=off
```

Disables all checks and re-enables the interactive API docs. The CLI refuses
`--no-auth` on anything but a loopback address. Only the exact value `off`
disables auth; any other `PYROMETER_AUTH` value (typos included) keeps it on.

## Transport and browser notes

- API keys travel in a header. Over plain HTTP they are visible to anyone on the
  network path, so use HTTPS (for example via a TLS-terminating proxy) for anything
  beyond a network you trust.
- The session cookie is `HttpOnly` and `SameSite=Strict`, and `Secure` when the
  request arrived over HTTPS. It grants read access only and is never accepted for writes.
- There is no CORS middleware; the dashboard is same-origin.

## Threat model

Protects against: unauthenticated reads or writes, a stolen database or keys
file, a leaked ingest key being used to read data, timing attacks on key
comparison, and cross-site requests from other websites.

Does not protect against: key leakage from your environment (shell history, CI
logs, committed notebooks — use one key per project and revoke on suspicion),
cleartext interception on plain HTTP, or compromise of the host running the server.
