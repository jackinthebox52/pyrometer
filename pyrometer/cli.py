import sys


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "serve":
        _serve(args[1:] if args else [])
    else:
        print(f"Unknown command: {args[0]}", file=sys.stderr)
        sys.exit(1)


def _serve(args: list[str]) -> None:
    import uvicorn

    host = "0.0.0.0"
    port = 8321

    i = 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
        elif args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        else:
            i += 1

    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")

    print(f"Pyrometer server → http://{host}:{port}")
    uvicorn.run("pyrometer.server.app:app", host=host, port=port, reload=False, log_level="info")
