"""Authentication for the Pyrometer server.

Three ways to prove identity, checked in this order:

1. ``Authorization: Bearer pyro_<id>_<secret>`` — an API key (SDK and scripts).
2. A trusted reverse proxy's identity header (e.g. authentik's ``X-authentik-username``).
   Only honoured when the TCP peer is listed in ``PYROMETER_TRUSTED_PROXIES``. Read-only.
3. The ``pyrometer_key`` cookie set by ``POST /auth/login`` (dashboard without a proxy).
   Read-only, safe methods and WebSocket only.

Auth is **on by default**. It is disabled only by ``PYROMETER_AUTH=off`` (``pyrometer serve
--no-auth``), which the CLI restricts to loopback.

API keys are 256-bit random secrets. Only their SHA-256 hash is stored (a slow KDF buys
nothing against a uniformly random 256-bit secret), comparisons are constant-time, and the
full key is shown exactly once, at creation.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, WebSocketException
from starlette.requests import HTTPConnection

SCOPES = ("ingest", "read")
COOKIE_NAME = "pyrometer_key"
_KEY_PREFIX = "pyro"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PUBLIC_PATHS = frozenset({"/health", "/auth/session", "/auth/login", "/auth/logout"})
# Hash compared against when the key id is unknown, so lookups take the same time either way.
_DUMMY_HASH = hashlib.sha256(b"pyrometer-dummy").hexdigest()


@dataclass(frozen=True)
class Principal:
    """An authenticated caller."""

    name: str
    scopes: frozenset[str]
    via: str  # "key" | "proxy" | "cookie"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def auth_enabled() -> bool:
    """Auth is required unless ``PYROMETER_AUTH`` is exactly ``off`` (fails closed on typos)."""
    return os.environ.get("PYROMETER_AUTH", "required").strip().lower() != "off"


def keys_path() -> Path:
    return Path(os.environ.get("PYROMETER_KEYS_FILE", str(Path.home() / ".pyrometer" / "keys.json")))


def _trusted_proxies() -> list[ipaddress._BaseNetwork]:
    nets: list[ipaddress._BaseNetwork] = []
    for part in os.environ.get("PYROMETER_TRUSTED_PROXIES", "").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def _proxy_user_header() -> str:
    return os.environ.get("PYROMETER_PROXY_USER_HEADER", "X-authentik-username")


# ---------------------------------------------------------------------------
# Key store (JSON file, 0600, replaced atomically so the CLI can edit it while
# the server runs — the DuckDB file is single-writer, so keys don't live there)
# ---------------------------------------------------------------------------

_cache: tuple[tuple[int, int], list[dict]] | None = None


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _load_keys() -> list[dict]:
    global _cache
    path = keys_path()
    try:
        st = path.stat()
    except FileNotFoundError:
        _cache = None
        return []
    sig = (st.st_mtime_ns, st.st_size)
    if _cache is not None and _cache[0] == sig:
        return _cache[1]
    keys = json.loads(path.read_text()).get("keys", [])
    _cache = (sig, keys)
    return keys


def _save_keys(keys: list[dict]) -> None:
    path = keys_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"keys": keys}, f, indent=2)
    os.replace(tmp, path)


def create_key(name: str, scopes: list[str]) -> tuple[str, str]:
    """Create an API key. Returns ``(key_id, full_key)``; the full key is never stored."""
    bad = [s for s in scopes if s not in SCOPES]
    if bad or not scopes:
        raise ValueError(f"scopes must be a non-empty subset of {SCOPES}, got {scopes!r}")
    keys = _load_keys()
    key_id = secrets.token_hex(4)
    while any(k["id"] == key_id for k in keys):
        key_id = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    keys = keys + [{
        "id": key_id,
        "name": name,
        "scopes": sorted(set(scopes)),
        "hash": _hash_secret(secret),
        "created_at": time.time(),
    }]
    _save_keys(keys)
    return key_id, f"{_KEY_PREFIX}_{key_id}_{secret}"


def list_keys() -> list[dict]:
    """Key metadata (never hashes or secrets)."""
    return [{k: v for k, v in rec.items() if k != "hash"} for rec in _load_keys()]


def revoke_key(key_id: str) -> bool:
    keys = _load_keys()
    remaining = [k for k in keys if k["id"] != key_id]
    if len(remaining) == len(keys):
        return False
    _save_keys(remaining)
    return True


def verify_key(token: str) -> Principal | None:
    """Validate a full API key; returns its principal or ``None``."""
    parts = token.split("_", 2)
    record = None
    secret = ""
    if len(parts) == 3 and parts[0] == _KEY_PREFIX:
        secret = parts[2]
        record = next((k for k in _load_keys() if k["id"] == parts[1]), None)
    expected = record["hash"] if record else _DUMMY_HASH
    matched = hmac.compare_digest(_hash_secret(secret), expected)
    if record is None or not matched:
        return None
    return Principal(name=record["name"], scopes=frozenset(record["scopes"]), via="key")


# ---------------------------------------------------------------------------
# Request authentication
# ---------------------------------------------------------------------------

def _peer_is_trusted_proxy(conn: HTTPConnection) -> bool:
    if conn.client is None:
        return False
    try:
        peer = ipaddress.ip_address(conn.client.host)
    except ValueError:
        return False
    return any(peer in net for net in _trusted_proxies())


def is_https(conn: HTTPConnection) -> bool:
    """True if the client connection is HTTPS, directly or via a trusted proxy."""
    if conn.url.scheme in ("https", "wss"):
        return True
    return _peer_is_trusted_proxy(conn) and conn.headers.get("x-forwarded-proto") == "https"


def authenticate(conn: HTTPConnection, read_only: bool) -> Principal | None:
    """Identify the caller. Proxy and cookie identities are only considered when ``read_only``."""
    header = conn.headers.get("authorization")
    if header is not None:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return verify_key(token.strip())  # a presented-but-bad key never falls through

    if not read_only:
        return None

    if _peer_is_trusted_proxy(conn):
        user = conn.headers.get(_proxy_user_header())
        if user:
            return Principal(name=user, scopes=frozenset({"read"}), via="proxy")

    cookie = conn.cookies.get(COOKIE_NAME)
    if cookie:
        principal = verify_key(cookie)
        if principal is not None:
            return Principal(name=principal.name, scopes=principal.scopes & {"read"}, via="cookie")
    return None


async def require_auth(conn: HTTPConnection) -> None:
    """App-wide dependency: every API route requires auth unless it is in ``_PUBLIC_PATHS``."""
    if not auth_enabled() or conn.scope["path"] in _PUBLIC_PATHS:
        return
    is_ws = conn.scope["type"] == "websocket"
    read_only = is_ws or conn.scope["method"] in _SAFE_METHODS
    need = "read" if read_only else "ingest"

    principal = authenticate(conn, read_only)
    if principal is not None and need in principal.scopes:
        return
    if is_ws:
        raise WebSocketException(code=1008)
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail={"data": None, "error": "Authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise HTTPException(
        status_code=403,
        detail={"data": None, "error": f"API key lacks the '{need}' scope"},
    )
