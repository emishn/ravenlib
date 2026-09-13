import argparse


try:
    from . import web_client
except ImportError:
    import web_client

try:
    from .app_info import APP_VERSION
except ImportError:
    from app_info import APP_VERSION


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"RavenLib desktop-style web client v{APP_VERSION}")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-window", action="store_true", help="Start only the local web server")
    parser.add_argument("--browser", default="embedded", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.no_window:
        server, _app_url = web_client.start_local_server(
            args.host,
            args.port,
            auto_shutdown_on_last_session_close=False,
        )
        web_client.serve_local_server(server)
        return

    web_client.run_embedded_window(args.host, args.port)


if __name__ == "__main__":
    main()
