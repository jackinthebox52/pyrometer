import argparse
import datetime
import ipaddress
import os
import sys

from pyrometer.server import auth


def main() -> None:
    parser = argparse.ArgumentParser(prog="pyrometer", description="Pyrometer experiment dashboard")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the server (default command)")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8321)
    serve.add_argument(
        "--no-auth",
        action="store_true",
        help="local mode: disable authentication (loopback only)",
    )

    keys = sub.add_parser("keys", help="manage API keys (for scripts and the SDK)")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    create = keys_sub.add_parser("create", help="create an API key (shown once)")
    create.add_argument("--name", required=True, help="label, e.g. the project or machine")
    create.add_argument("--project", help="bind the key to one existing project (default: global key)")
    keys_sub.add_parser("list", help="list keys (metadata only)")
    revoke = keys_sub.add_parser("revoke", help="revoke a key by id")
    revoke.add_argument("key_id")

    admin = sub.add_parser("admin", help="manage dashboard accounts from the host")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    setpw = admin_sub.add_parser(
        "set-password",
        help="create or reset a dashboard user (random password, printed once, unless --password-stdin); "
        "ends that user's sessions",
    )
    setpw.add_argument("--user", default="admin", help="username (default: admin)")
    setpw.add_argument("--role", choices=auth.ROLES, help="role for a new user (default: admin if first, else user)")
    setpw.add_argument("--password-stdin", action="store_true", help="read the new password from stdin")

    sync = sub.add_parser(
        "sync",
        help="upload runs saved offline or left undelivered (spool) to a server; deletes them on success",
    )
    sync.add_argument(
        "target", nargs="?",
        help="spool directory or run id (default: every run in ~/.pyrometer/spool or $PYROMETER_SPOOL_DIR)",
    )
    sync.add_argument("--server", help="server URL (default: $PYROMETER_SERVER, then the URL recorded in the spool)")

    args = parser.parse_args()
    if args.command in (None, "serve"):
        if args.command is None:
            args = serve.parse_args([])
        _serve(args)
    elif args.command == "keys":
        _keys(args)
    elif args.command == "sync":
        _sync(args)
    else:
        _admin(args)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _serve(args: argparse.Namespace) -> None:
    import logging

    import uvicorn

    if args.no_auth:
        if not _is_loopback(args.host):
            sys.exit(f"Refusing --no-auth on non-loopback host {args.host!r}: bind 127.0.0.1 or enable auth.")
        os.environ["PYROMETER_AUTH"] = "off"

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")
    print(f"Pyrometer server → http://{args.host}:{args.port}  (auth: {'off' if args.no_auth else 'required'})")
    # proxy_headers=False: auth decides trust from the real TCP peer, so uvicorn must not rewrite it.
    uvicorn.run(
        "pyrometer.server.app:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
        proxy_headers=False,
    )


def _sync(args: argparse.Namespace) -> None:
    from pyrometer._sync import find_spools, sync_run_dir

    dirs = find_spools(args.target)
    if not dirs:
        print("Nothing to sync.")
        return
    failed = False
    for d in dirs:
        r = sync_run_dir(d, server=args.server)
        for m in r.messages:
            print(f"  {m}")
        if r.skipped:
            print(f"{r.run_id}: skipped ({r.error})")
        elif r.complete:
            print(f"{r.run_id}: synced {r.sent} payloads")
        else:
            failed = True
            print(f"{r.run_id}: {r.sent} sent, {r.remaining} left in {d} ({r.error})")
    if failed:
        sys.exit(1)


def _keys(args: argparse.Namespace) -> None:
    if args.keys_command == "create":
        if args.project is not None:
            from pyrometer.server.db import db_get_project

            if db_get_project(args.project) is None:
                sys.exit(f"No such project {args.project!r}")
        key_id, key = auth.create_key(args.name, project=args.project)
        print(f"Created {'project ' + args.project if args.project else 'global'} key {key_id} ({args.name})")
        print("\nThis is the only time the full key is shown. Store it in a secret manager:\n")
        print(f"  {key}\n")
        print("Use it with:  export PYROMETER_API_KEY=<key>")
    elif args.keys_command == "list":
        rows = auth.list_keys()
        if not rows:
            print("No API keys. Create one with: pyrometer keys create --name <name>")
        for r in rows:
            created = datetime.datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
            print(f"{r['id']}  {r['name']:<24} {r['project'] or 'global':<16} created {created}")
    elif args.keys_command == "revoke":
        if not auth.revoke_key(args.key_id):
            sys.exit(f"No key with id {args.key_id!r}")
        print(f"Revoked key {args.key_id}")


def _admin(args: argparse.Namespace) -> None:
    import secrets

    if args.password_stdin:
        password, generated = sys.stdin.readline().rstrip("\n"), False
    else:
        password, generated = secrets.token_urlsafe(18), True
    try:
        auth.set_password(args.user, password, args.role)
    except ValueError as e:
        sys.exit(str(e))
    print(f"Password set for {args.user.strip().lower()}; their dashboard sessions were ended.")
    if generated:
        print(f"\nThis is the only time it is shown:\n\n  {password}\n")
