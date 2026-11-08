"""``python -m projektakte.healthcheck`` – Docker HEALTHCHECK without curl: exit code 0 if
``/health/ready`` of the local server answers 200 (``ok`` or ``degraded``), otherwise 1."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def check(port: int | str | None = None, timeout: float = 4.0) -> tuple[bool, str]:
    port = port or os.environ.get("PORT") or "8000"
    url = f"http://127.0.0.1:{port}/health/ready"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via HTTP(S)_PROXY
    try:
        with opener.open(url, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        return False, f"nicht bereit (HTTP {exc.code})"
    except (OSError, ValueError) as exc:
        return False, f"nicht erreichbar ({exc})"
    return True, str(data.get("status", "ok"))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    ok, message = check(args[0] if args else None)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
