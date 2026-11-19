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

    keys = sub.add_parser("keys", help="manage API keys")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    create = keys_sub.add_parser("create", help="create an API key (shown once)")
    create.add_argument("--name", required=True, help="label, e.g. the project or machine")
    create.add_argument(
        "--scopes",
        default="ingest",
        help="comma-separated: ingest (SDK writes), read (dashboard/API reads). Default: ingest",
    )
    keys_sub.add_parser("list", help="list keys (metadata only)")
    revoke = keys_sub.add_parser("revoke", help="revoke a key by id")
    revoke.add_argument("key_id")

    args = parser.parse_args()
    if args.command in (None, "serve"):
        if args.command is None:
            args = serve.parse_args([])
        _serve(args)
    else:
        _keys(args)


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


def _keys(args: argparse.Namespace) -> None:
    if args.keys_command == "create":
        scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
        try:
            key_id, key = auth.create_key(args.name, scopes)
        except ValueError as e:
            sys.exit(str(e))
        print(f"Created key {key_id} ({args.name}, scopes: {','.join(scopes)})")
        print("\nThis is the only time the full key is shown. Store it in a secret manager:\n")
        print(f"  {key}\n")
        print("Use it with:  export PYROMETER_API_KEY=<key>")
    elif args.keys_command == "list":
        rows = auth.list_keys()
        if not rows:
            print("No API keys. Create one with: pyrometer keys create --name <name>")
        for r in rows:
            created = datetime.datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
            print(f"{r['id']}  {r['name']:<24} {','.join(r['scopes']):<12} created {created}")
    elif args.keys_command == "revoke":
        if not auth.revoke_key(args.key_id):
            sys.exit(f"No key with id {args.key_id!r}")
        print(f"Revoked key {args.key_id}")
