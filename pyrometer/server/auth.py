"""Authentication and authorization for the Pyrometer server.

Callers (``Principal.role``):

* **admin** — a dashboard user with the admin role. Sees every project, manages users, projects
  and API keys. Browser sessions are read-only for run data: they can never write metrics.
* **user** — a dashboard user with the regular role. Reads public projects plus private projects
  they are a member of. Cannot write run data or manage anything.
* **global** — an API key not bound to a project. Reads and writes every project and may register
  new projects, but cannot manage users, keys or membership.
* **project** — an API key bound to one project. Reads and writes only that project.

Auth is **on by default**. It is disabled only by ``PYROMETER_AUTH=off`` (``pyrometer serve
--no-auth``), which the CLI restricts to loopback.

First run: with no admin the server prints a one-time setup token to its log; the dashboard's
setup screen exchanges it for the first admin account. ``pyrometer admin set-password`` does the
same from the host. Credentials live in one JSON file (``PYROMETER_AUTH_FILE``, mode 0600); projects
and their membership live in the DuckDB database next to the runs they describe.

Secrets at rest: API keys and session tokens are 256-bit random values stored only as SHA-256
hashes (a slow KDF buys nothing against uniform 256-bit secrets); user passwords use scrypt. All
comparisons are constant-time.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException, WebSocketException
from starlette.requests import HTTPConnection

from pyrometer.server import db

try:  # cross-process lock so the CLI and the server can both edit the file
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

COOKIE_NAME = "pyrometer_session"
MIN_PASSWORD_LEN = 6
SESSION_TTL = 30 * 24 * 3600
_KEY_PREFIX = "pyro"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PUBLIC_PATHS = frozenset({"/health", "/auth/session", "/auth/setup", "/auth/login", "/auth/logout"})
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ROLES = ("admin", "user")
_DUMMY_HASH = hashlib.sha256(b"pyrometer-dummy").hexdigest()

# scrypt: N=2^15, r=8, p=1 (~32 MiB, ~100 ms)
_SCRYPT_LOG_N, _SCRYPT_R, _SCRYPT_P = 15, 8, 1

# Login/setup throttle: this many failures within the window blocks further attempts.
_MAX_FAILURES = 10
_FAILURE_WINDOW = 600.0


class RateLimited(Exception):
    """Too many failed login/setup attempts."""


@dataclass(frozen=True)
class Principal:
    """An authenticated caller."""

    name: str
    role: str  # "admin" | "user" | "global" | "project"
    via: str  # "key" | "proxy" | "session"
    project: str | None = None  # set for role == "project"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def auth_enabled() -> bool:
    """Auth is required unless ``PYROMETER_AUTH`` is exactly ``off`` (fails closed on typos)."""
    return os.environ.get("PYROMETER_AUTH", "required").strip().lower() != "off"


def auth_path() -> Path:
    return Path(os.environ.get("PYROMETER_AUTH_FILE", str(Path.home() / ".pyrometer" / "auth.json")))


def _trusted_proxies() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for part in os.environ.get("PYROMETER_TRUSTED_PROXIES", "").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def _proxy_user_header() -> str:
    return os.environ.get("PYROMETER_PROXY_USER_HEADER", "X-authentik-username")


# ---------------------------------------------------------------------------
# Auth file: {"users": [...], "keys": [...], "sessions": [...]}
# Replaced atomically; read-modify-write is serialised across threads and processes.
# ---------------------------------------------------------------------------

_cache: tuple[tuple[int, int], dict] | None = None
_thread_lock = threading.Lock()


def _empty() -> dict:
    return {"users": [], "keys": [], "sessions": []}


def _normalize(data: dict) -> dict:
    """Upgrade files written before user accounts (a single ``admin`` password) in memory."""
    legacy = data.pop("admin", None)
    if legacy and not data["users"]:
        data["users"] = [{
            "username": "admin",
            "password_hash": legacy["password_hash"],
            "role": "admin",
            "created_at": legacy.get("updated_at", time.time()),
        }]
        data["sessions"] = []  # old sessions carry no username
    return data


def _read() -> dict:
    """Current contents (cached by mtime/size, so CLI edits are picked up without a restart)."""
    global _cache
    path = auth_path()
    try:
        st = path.stat()
    except FileNotFoundError:
        _cache = None
        return _empty()
    sig = (st.st_mtime_ns, st.st_size)
    if _cache is not None and _cache[0] == sig:
        return _cache[1]
    data = _normalize({**_empty(), **json.loads(path.read_text())})
    _cache = (sig, data)
    return data


def _modify(fn: Callable[[dict], object]) -> object:
    """Apply ``fn`` to a fresh copy of the file under a lock, then write it back atomically."""
    global _cache
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _thread_lock:
        lock_fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _cache = None
            data = json.loads(json.dumps(_read()))  # deep copy
            result = fn(data)
            tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
            _cache = None
            return result
        finally:
            os.close(lock_fd)  # also releases the flock


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Admin password
# ---------------------------------------------------------------------------

def _scrypt(password: str, salt: bytes, log_n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=2**log_n, r=r, p=p, dklen=32, maxmem=256 * 1024 * 1024
    )


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, _SCRYPT_LOG_N, _SCRYPT_R, _SCRYPT_P)
    return f"scrypt${_SCRYPT_LOG_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, log_n, r, p, salt, digest = stored.split("$")
        got = _scrypt(password, bytes.fromhex(salt), int(log_n), int(r), int(p))
        return hmac.compare_digest(got, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def admin_configured() -> bool:
    return any(u["role"] == "admin" for u in _read()["users"])


def _clean_username(username: str) -> str:
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise ValueError("username must be 2-32 characters: letters, digits, '.', '_' or '-'")
    return username


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")


def list_users() -> list[dict]:
    """User metadata (never password hashes)."""
    return [
        {"username": u["username"], "role": u["role"], "created_at": u["created_at"]}
        for u in _read()["users"]
    ]


def get_user(username: str) -> dict | None:
    return next((u for u in _read()["users"] if u["username"] == username.strip().lower()), None)


def create_user(username: str, password: str, role: str = "user") -> None:
    """Create an account. Raises ``ValueError`` for a bad name/password/role or a taken username."""
    username = _clean_username(username)
    _check_password(password)
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    hashed = _hash_password(password)

    def apply(data: dict) -> None:
        if any(u["username"] == username for u in data["users"]):
            raise ValueError(f"user {username!r} already exists")
        data["users"].append(
            {"username": username, "password_hash": hashed, "role": role, "created_at": time.time()}
        )

    _modify(apply)


def set_password(username: str, password: str, role: str | None = None) -> None:
    """Reset a user's password (ends their sessions), creating the account if it does not exist.

    A new account gets ``role`` (default ``admin`` if it is the first user, else ``user``).
    """
    username = _clean_username(username)
    _check_password(password)
    if role is not None and role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    hashed = _hash_password(password)

    def apply(data: dict) -> None:
        user = next((u for u in data["users"] if u["username"] == username), None)
        if user is None:
            new_role = role or ("admin" if not data["users"] else "user")
            data["users"].append(
                {"username": username, "password_hash": hashed, "role": new_role, "created_at": time.time()}
            )
        else:
            user["password_hash"] = hashed
            if role is not None:
                _guard_last_admin(data, username, role)
                user["role"] = role
        data["sessions"] = [s for s in data["sessions"] if s["username"] != username]

    _modify(apply)


def _guard_last_admin(data: dict, username: str, new_role: str | None) -> None:
    """Refuse a change that would leave the instance without any admin."""
    others = [u for u in data["users"] if u["role"] == "admin" and u["username"] != username]
    if not others and new_role != "admin":
        raise ValueError("cannot remove the last admin")


def update_user_role(username: str, role: str) -> bool:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    username = username.strip().lower()

    def apply(data: dict) -> bool:
        user = next((u for u in data["users"] if u["username"] == username), None)
        if user is None:
            return False
        _guard_last_admin(data, username, role)
        user["role"] = role
        return True

    return bool(_modify(apply))


def delete_user(username: str) -> bool:
    """Delete an account and end its sessions. Refuses to delete the last admin."""
    username = username.strip().lower()

    def apply(data: dict) -> bool:
        user = next((u for u in data["users"] if u["username"] == username), None)
        if user is None:
            return False
        _guard_last_admin(data, username, None)
        data["users"] = [u for u in data["users"] if u["username"] != username]
        data["sessions"] = [s for s in data["sessions"] if s["username"] != username]
        return True

    deleted = bool(_modify(apply))
    if deleted:
        db.db_remove_user_memberships(username)
    return deleted


def change_own_password(username: str, current: str, new: str) -> None:
    """Self-service change; verifies ``current`` first. Raises ``PermissionError`` if it is wrong."""
    _check_password(new)
    _check_throttle()
    user = get_user(username)
    if user is None or not _verify_password(current, user["password_hash"]):
        _record_failure()
        raise PermissionError("Current password is incorrect")
    set_password(username, new)


# ---------------------------------------------------------------------------
# Throttle (in-memory, global: behind a proxy there is no trustworthy client address)
# ---------------------------------------------------------------------------

_failures: list[float] = []


def _check_throttle() -> None:
    now = time.monotonic()
    _failures[:] = [t for t in _failures if now - t < _FAILURE_WINDOW]
    if len(_failures) >= _MAX_FAILURES:
        raise RateLimited()


def _record_failure() -> None:
    _failures.append(time.monotonic())


# ---------------------------------------------------------------------------
# First-run setup token (in memory only; printed to the server log)
# ---------------------------------------------------------------------------

_setup_token: str | None = None
_setup_lock = threading.RLock()


def setup_token() -> str | None:
    """The one-time setup token, or ``None`` if an admin already exists.

    ``PYROMETER_BOOTSTRAP_TOKEN`` pins it (for secret managers); otherwise a random one is
    generated per server process.
    """
    global _setup_token
    if admin_configured():
        return None
    pinned = os.environ.get("PYROMETER_BOOTSTRAP_TOKEN")
    if pinned:
        return pinned
    with _setup_lock:
        if _setup_token is None:
            _setup_token = secrets.token_urlsafe(24)
        return _setup_token


def claim_setup(token: str, username: str, password: str) -> str:
    """Create the first admin if ``token`` is the live setup token; returns the username.

    Raises ``PermissionError`` for a wrong token or if setup is already done, ``RateLimited`` after
    too many failures, ``ValueError`` for a bad username or weak password.
    """
    global _setup_token
    _check_throttle()
    with _setup_lock:
        expected = setup_token()
        if expected is None or not hmac.compare_digest(token.encode(), expected.encode()):
            _record_failure()
            raise PermissionError("Invalid setup token, or setup is already complete")
        username = _clean_username(username)
        create_user(username, password, "admin")
        _setup_token = None
        return username


# ---------------------------------------------------------------------------
# Admin sessions
# ---------------------------------------------------------------------------

def login(username: str, password: str) -> str:
    """Verify a user's password and return a new session token (raw; shown to the browser once)."""
    _check_throttle()
    user = get_user(username)
    if user is None:  # burn the same time as a real check
        _verify_password(password, _hash_password("pyrometer-dummy"))
    ok = user is not None and _verify_password(password, user["password_hash"])
    if not ok:
        _record_failure()
        raise PermissionError("Invalid username or password")
    username = user["username"]
    _failures.clear()
    token = secrets.token_urlsafe(32)
    now = time.time()

    def apply(data: dict) -> None:
        data["sessions"] = [s for s in data["sessions"] if s["expires_at"] > now]
        data["sessions"].append(
            {"hash": _sha256(token), "username": username, "created_at": now, "expires_at": now + SESSION_TTL}
        )

    _modify(apply)
    return token


def _session_user(token: str) -> dict | None:
    """The user record a live session token belongs to (current role, so changes apply at once)."""
    now = time.time()
    h = _sha256(token)
    for sess in _read()["sessions"]:
        if hmac.compare_digest(sess["hash"], h) and sess["expires_at"] > now:
            return get_user(sess["username"])
    return None


def _session_valid(token: str) -> bool:
    return _session_user(token) is not None


def end_session(token: str | None) -> None:
    if not token:
        return
    h = _sha256(token)
    _modify(lambda data: data.update(sessions=[s for s in data["sessions"] if s["hash"] != h]))


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def create_key(name: str, project: str | None = None) -> tuple[str, str]:
    """Create an API key, global or bound to ``project``. Returns ``(key_id, full_key)``.

    The full key is never stored.
    """
    secret = secrets.token_urlsafe(32)

    def apply(data: dict) -> str:
        key_id = secrets.token_hex(4)
        while any(k["id"] == key_id for k in data["keys"]):
            key_id = secrets.token_hex(4)
        data["keys"].append({
            "id": key_id,
            "name": name,
            "project": project,
            "hash": _sha256(secret),
            "created_at": time.time(),
        })
        return key_id

    key_id = _modify(apply)
    return key_id, f"{_KEY_PREFIX}_{key_id}_{secret}"  # type: ignore[return-value]


def list_keys() -> list[dict]:
    """Key metadata (never hashes or secrets)."""
    return [{"project": None, **{k: v for k, v in rec.items() if k != "hash"}} for rec in _read()["keys"]]


def revoke_key(key_id: str) -> bool:
    def apply(data: dict) -> bool:
        before = len(data["keys"])
        data["keys"] = [k for k in data["keys"] if k["id"] != key_id]
        return len(data["keys"]) < before

    return bool(_modify(apply))


def _verify_key(token: str) -> Principal | None:
    parts = token.split("_", 2)
    record = None
    secret = ""
    if len(parts) == 3 and parts[0] == _KEY_PREFIX:
        secret = parts[2]
        record = next((k for k in _read()["keys"] if k["id"] == parts[1]), None)
    expected = record["hash"] if record else _DUMMY_HASH
    matched = hmac.compare_digest(_sha256(secret), expected)  # same work for unknown ids
    if record is None or not matched:
        return None
    project = record.get("project")
    return Principal(
        name=record["name"], role="project" if project else "global", via="key", project=project
    )


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


def _same_origin(conn: HTTPConnection) -> bool:
    """CSRF defence in depth for browser-authenticated state changes: Origin must match our Host."""
    origin = conn.headers.get("origin")
    if not origin:
        return False
    allowed = {conn.headers.get("host", "")}
    if _peer_is_trusted_proxy(conn):
        allowed.add(conn.headers.get("x-forwarded-host", ""))
    allowed.discard("")
    return urlparse(origin).netloc in allowed


def authenticate(conn: HTTPConnection) -> Principal | None:
    """Identify the caller: API key, then trusted-proxy identity, then admin session cookie."""
    header = conn.headers.get("authorization")
    if header is not None:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return _verify_key(token.strip())  # a presented-but-bad key never falls through

    if _peer_is_trusted_proxy(conn):
        user = conn.headers.get(_proxy_user_header())
        if user:
            return Principal(name=user, role="admin", via="proxy")

    cookie = conn.cookies.get(COOKIE_NAME)
    if cookie:
        account = _session_user(cookie)
        if account is not None:
            return Principal(name=account["username"], role=account["role"], via="session")
    return None


# ---------------------------------------------------------------------------
# Project access policy
# ---------------------------------------------------------------------------

def can_read_project(principal: Principal | None, project: str) -> bool:
    """``principal is None`` means auth is off (local mode)."""
    if principal is None or principal.role in ("admin", "global"):
        return True
    if principal.role == "project":
        return principal.project == project
    row = db.db_get_project(project)
    return row is not None and (row["visibility"] == "public" or db.db_is_member(project, principal.name))


def can_write_project(principal: Principal | None, project: str) -> bool:
    """Only API keys write run data; dashboard users and proxy identities never do."""
    if principal is None or principal.role == "global":
        return True
    return principal.role == "project" and principal.project == project


def visible_projects(principal: Principal | None) -> list[str] | None:
    """Names of readable projects, or ``None`` for no restriction."""
    if principal is None or principal.role in ("admin", "global"):
        return None
    if principal.role == "project":
        return [principal.project]  # type: ignore[list-item]
    return db.db_user_project_names(principal.name)


# ---------------------------------------------------------------------------
# Request gate
# ---------------------------------------------------------------------------

def _deny(is_ws: bool, status: int, message: str) -> None:
    if is_ws:
        raise WebSocketException(code=1008)
    raise HTTPException(status_code=status, detail={"data": None, "error": message})


async def require_auth(conn: HTTPConnection) -> None:
    """App-wide dependency: every API route requires auth unless it is in ``_PUBLIC_PATHS``.

    Coarse rules, enforced here for every route (default-deny):

    * ``/admin/*``: admins only; state changes must be same-origin.
    * ``POST /projects``: global keys or admins (admins must be same-origin).
    * ``POST /auth/password``: any signed-in dashboard user (same-origin).
    * every other write: API keys only; dashboard sessions are read-only for run data.

    Object-level rule: any route with a ``run_id`` is checked against that run's project (404 when
    the caller may not see it, so private runs do not even reveal their existence).
    """
    path = conn.scope["path"]
    if not auth_enabled() or path in _PUBLIC_PATHS:
        return
    is_ws = conn.scope["type"] == "websocket"
    method = "GET" if is_ws else conn.scope["method"]
    safe = method in _SAFE_METHODS

    principal = authenticate(conn)
    if principal is None:
        if is_ws:
            raise WebSocketException(code=1008)
        raise HTTPException(
            status_code=401,
            detail={"data": None, "error": "Authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    conn.state.principal = principal

    browser = principal.via != "key"
    if path == "/admin" or path.startswith("/admin/"):
        if principal.role != "admin":
            _deny(is_ws, 403, "Admin access required")
        if not safe and not _same_origin(conn):
            _deny(is_ws, 403, "Cross-origin request refused")
    elif not safe and path == "/projects":
        if principal.role not in ("global", "admin"):
            _deny(is_ws, 403, "Registering a project needs a global API key or an admin")
        if browser and not _same_origin(conn):
            _deny(is_ws, 403, "Cross-origin request refused")
    elif not safe and path == "/auth/password":
        if not browser or principal.via == "proxy":
            _deny(is_ws, 403, "Only signed-in dashboard users can change their password")
        if not _same_origin(conn):
            _deny(is_ws, 403, "Cross-origin request refused")
    elif not safe and browser:
        _deny(is_ws, 403, "Writes require an API key; dashboard sessions are read-only")

    run_id = conn.scope.get("path_params", {}).get("run_id")
    if run_id:
        project = db.db_get_run_project(run_id)
        if project is not None:  # a missing run falls through to the route's own 404
            allowed = can_read_project(principal, project) if safe else can_write_project(principal, project)
            if not allowed:
                _deny(is_ws, 404, "Run not found")
