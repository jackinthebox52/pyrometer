# Authentication, users and projects

Pyrometer requires authentication by default. Nothing is readable or writable
without a credential except `GET /health`, the dashboard's static files, and the
`/auth/*` session endpoints.

## Who can do what

| | Credential | Runs | Projects | Users, keys, membership |
|--|-----------|------|----------|------------------------|
| **Admin** | Username + password (or authentik) | Reads everything. Dashboard sessions never write run data | Sees all, registers, edits visibility and members | Manages all |
| **User** | Username + password | Reads public projects and private ones they are a member of | Sees the same set | Changes own password only |
| **Global key** | `Authorization: Bearer pyro_…` | Reads **and writes** every project, private included | Sees all, registers new ones | Nothing |
| **Project key** | `Authorization: Bearer pyro_…` | Reads and writes **only its own project** | Sees only its own | Nothing |

Users and admins are dashboard accounts. Keys are for scripts. Keys can't manage users, keys or
membership, so a leaked training-script key can't mint replacements or grant itself access.

## Projects

Every run belongs to a project. Runs with no project go to the built-in public project `default`.

- **Public:** every signed-in user can see it.
- **Private:** only admins, its members, and keys for it (the global key and its own project key) can see it.
  A private run's existence isn't revealed to others: requests for it return `404`.

Registering a project:

- A global key can register one through the API. Registering a **private** project also mints that project's
  own key, returned once. No admin is involved.
- Using an unknown project name with a global key registers it as **public** on first use.
- Admins register projects and flip visibility in the dashboard (Settings → Projects).

```python
info = pyrometer.create_project("team-a", private=True)   # needs a global key
# info["key"] is the project's key, shown once. Scripts for team-a use it:
#   PYROMETER_API_KEY=<that key>  ->  pyrometer.init(name="x")   # project implied
run = pyrometer.init(name="baseline", project="team-a")           # or name it explicitly
```

A project key with no `project=` argument logs to its own project. Naming any other project fails with
`LookupError`. Existing databases are migrated on startup: all prior runs go to `default`.

Membership is managed by admins (Settings → Projects → members). It only matters for **private**
projects; public ones are already visible to every user. Changes apply immediately, including to open
dashboards and live WebSocket updates, which are filtered per user.

## First-run setup

With no admin configured, the server prints a one-time setup token to its log:

```
WARNING pyrometer.server — Pyrometer is not set up. Open the dashboard and enter this one-time setup token: …
```

1. Read it: `docker compose logs pyrometer | grep "setup token"` (or the terminal running `pyrometer serve`).
2. Open the dashboard. The setup screen asks for the token plus a username and password (6+ characters) for the first admin.
3. The token is then dead. Add users, projects and API keys under **Settings**.

Why a token: without it, whoever reached the page first on a shared network could claim the server. It lives
only in the server's memory and log; restarting before setup issues a new one.

Alternatives:

- **From the host**, no token: `pyrometer admin set-password [--user NAME] [--role admin|user]` creates the user
  or resets their password, printing a random one (or reading stdin with `--password-stdin`). Having filesystem
  access to the auth file is the trust boundary. It ends that user's sessions.
- **Pinned token**, for secret managers and automation: set `PYROMETER_BOOTSTRAP_TOKEN`.
- **Behind authentik**: users the trusted proxy vouches for are admins immediately; no setup screen.

If you lose the admin password: `pyrometer admin set-password` on the host.

## Alert webhooks

Alert rules (Settings → Alerts, `/admin/alerts`) are admin-only. Webhook URLs usually embed a token, so they
are treated as secrets: never logged, masked to the host in every response, and never sent to non-admins or
SDK keys. Alert messages contain only the run name, project and an optional link, so they reveal nothing a
rule's webhook owner (an admin) could not already see.

## API keys

Create them in Settings → API keys (global, or bound to one project), or on the host:

```bash
pyrometer keys create --name my-laptop                    # global key, printed ONCE
pyrometer keys create --name ci --project team-a          # project key
pyrometer keys list
pyrometer keys revoke <id>
```

Changes take effect immediately; no restart is needed.

- 256-bit random secret, format `pyro_<id>_<secret>`.
- Only a SHA-256 hash is stored, in the auth file (mode `0600`; default `~/.pyrometer/auth.json`, override with
  `PYROMETER_AUTH_FILE`). The full key is shown once and can't be recovered. A leaked database or auth file
  yields no usable keys.
- Verification is constant-time, and an unknown key id costs the same as a wrong secret.
- A presented-but-invalid `Authorization` header is rejected outright; it never falls back to another credential.

### Using a key from the SDK

```bash
export PYROMETER_SERVER=http://pyrometer.example.com:8321
export PYROMETER_API_KEY=pyro_...        # from your secret store; never commit it
```

```python
run = pyrometer.init(name="baseline", project="team-a")
```

Or pass `server=` / `api_key=` explicitly. If the server rejects the key, `init()` raises `PermissionError`.

## Accounts and sessions

- Passwords use scrypt (N=2¹⁵, r=8, p=1). Usernames are case-insensitive. Session tokens are 256-bit random values,
  stored only as hashes, expire after 30 days, and end on logout, password change or reset, or account deletion.
  A role change applies to existing sessions immediately.
- The session cookie is `HttpOnly` and `SameSite=Strict`, and `Secure` when the request arrived over HTTPS.
- Failed logins are throttled: 10 failures in 10 minutes blocks further attempts (HTTP 429) until the window
  clears. The limit is global, since behind a proxy there is no trustworthy client address. API keys are unaffected.
- State-changing browser requests (anything under `/admin`, `POST /projects`, `POST /auth/password`) must be
  same-origin: `Origin` must match `Host` (or `X-Forwarded-Host` from a trusted proxy). This is on top of `SameSite=Strict`.
- The last admin can't be deleted or demoted, and you can't delete your own account.

## Reverse-proxy identity (authentik)

To put the dashboard behind an SSO proxy, tell Pyrometer which peers to trust and which header carries the username:

```bash
PYROMETER_TRUSTED_PROXIES=127.0.0.1,172.16.0.0/12   # IPs/CIDRs of your proxy as the server sees it
PYROMETER_PROXY_USER_HEADER=X-authentik-username    # default
```

The header is honoured **only** when the TCP peer is in `PYROMETER_TRUSTED_PROXIES` (uvicorn's `X-Forwarded-For`
rewriting is disabled so the real peer is used). Everyone your proxy authenticates is an **admin**, so restrict
access with an authentik policy. Your proxy **must** overwrite or clear that header on every request; see
[deployment.md](deployment.md#behind-nginx-and-authentik). Proxy identities never write run data; that always
needs an API key.

## Local mode

```bash
pyrometer serve --no-auth      # or PYROMETER_AUTH=off
```

Disables all checks (no project restrictions either) and re-enables the interactive API docs. The CLI refuses
`--no-auth` on anything but a loopback address. Only the exact value `off` disables auth; any other
`PYROMETER_AUTH` value (typos included) keeps it on.

## Transport and browser notes

- API keys and passwords travel over the wire. On plain HTTP anyone on the network path can read them, so use
  HTTPS (for example a TLS-terminating proxy) beyond a network you trust.
- There is no CORS middleware; the dashboard is same-origin.

## Threat model

Protects against: unauthenticated reads or writes, a stolen database or auth file, a leaked script key being used
to manage users or keys, one project's key or one user's account reaching another private project, claiming an
unconfigured server without its setup token, timing attacks on credential comparison, password guessing, and
cross-site requests from other websites.

Does not protect against: key leakage from your environment (use one key per project and revoke on suspicion),
a global key (it reaches every project by design; prefer project keys for anything shared), cleartext
interception on plain HTTP, weak passwords beyond the throttle, or compromise of the host running the server.
