"""Command-line connection check for the RavenLib demo server."""

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


DEFAULT_SERVER_URL = "http://127.0.0.1:8000"


def check_connection(server_url: str) -> int:
    url = f"{server_url.rstrip('/')}/api/connection/status"
    try:
        with urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Connection failed: {exc}")
        return 1
    print(payload.get("message", "Connection confirmed"))
    print(f"Server time: {payload.get('server_time')}")
    return 0 if payload.get("connected") else 1


if __name__ == "__main__":
    raise SystemExit(check_connection(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SERVER_URL))
