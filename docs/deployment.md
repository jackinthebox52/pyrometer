# Deployment

## Docker

The repo ships a `Dockerfile` (builds the dashboard, then a slim Python image —
no torch) and a `docker-compose.yml`.

```bash
docker compose up -d --build
docker compose exec pyrometer python -m pyrometer keys create --name my-project --scopes ingest
```

The compose file binds `127.0.0.1:8321` by default. To serve your LAN, change the
port mapping to `"0.0.0.0:8321:8321"` — and remember auth is required and keys
cross the wire in cleartext over plain HTTP.

Data lives in `./data` (`data.duckdb` and `keys.json`); that directory must be
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
        proxy_pass http://127.0.0.1:8321;
    }
}
```

Then set `PYROMETER_TRUSTED_PROXIES` to the address nginx connects from, as the server
sees it. With the compose file and nginx on the same host that is the Docker bridge
gateway (often `172.17.0.1`, or the subnet of your compose network); check
`docker network inspect`.

Even if nginx is misconfigured, a proxy identity only ever grants read access;
writes always require an API key.
