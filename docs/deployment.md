# Deployment

## Docker

The repo ships a `Dockerfile` (builds the dashboard, then a slim Python image —
no torch) and a `docker-compose.yml`.

```bash
docker compose up -d --build
docker compose logs pyrometer | grep "setup token"   # one-time token for first-run setup
```

Open the dashboard, enter the token and create the first admin, then add users, projects and API keys under
**Settings**. See [auth.md](auth.md#first-run-setup).

The compose file binds `127.0.0.1:8321` by default. To serve your LAN, change the
port mapping to `"0.0.0.0:8321:8321"` — and remember auth is required and keys
cross the wire in cleartext over plain HTTP.

Data lives in `./data` (`data.duckdb` and `auth.json`); that directory must be
writable by uid 1000, the container user. The container runs read-only with all
capabilities dropped. Back up `./data`.

Upgrading: pull the new source and `docker compose up -d --build`. Data and keys persist.

## Behind nginx and authentik

An example layout (adapt to your authentik version; see its proxy-provider docs for the
outpost locations). Use two hostnames:

- `pyrometer.example.com` — dashboard, authentik-protected, forwards the username.
- `pyrometer-api.example.com` — for the SDK; no authentik, API keys only. It **must**
  clear the identity header so clients cannot forge it.

```nginx
# Dashboard
server {
    server_name pyrometer.example.com;
    location / {
        auth_request     /outpost.goauthentik.io/auth/nginx;
        error_page       401 = @goauthentik_proxy_signin;
        auth_request_set $authentik_username $upstream_http_x_authentik_username;
        proxy_set_header X-authentik-username $authentik_username;   # overwrites any client value
        proxy_set_header Host $host;                                 # lets same-origin checks pass

        proxy_pass http://127.0.0.1:8321;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;                   # WebSocket (/ws)
        proxy_set_header Connection "upgrade";
    }
    # ... authentik outpost locations ...
}

# SDK ingest
server {
    server_name pyrometer-api.example.com;
    location / {
        proxy_set_header X-authentik-username "";                    # never trust a client-sent value
        proxy_set_header Host $host;
        proxy_pass http://127.0.0.1:8321;
    }
}
```

Then set `PYROMETER_TRUSTED_PROXIES` to the address nginx connects from, as the server
sees it. With the compose file and nginx on the same host that is the Docker bridge
gateway (often `172.17.0.1`, or the subnet of your compose network); check
`docker network inspect`.

Everyone authentik lets through is an admin (can manage users, projects and API keys), so scope access with an
authentik policy. Even if nginx is misconfigured, a proxy identity can't write run data; that always needs an API key.

## Alerts

Admins configure webhook alerts (crash, failure, severe events, metric thresholds) under **Settings → Alerts**.
Set `PYROMETER_PUBLIC_URL=https://pyrometer.example.com` on the server to put a dashboard link in each alert,
and optionally `PYROMETER_ALERT_RATE` (alerts per rule per minute, default 10). The server needs outbound
HTTP(S) access to the webhook. See [server-api.md](server-api.md#alerts).
