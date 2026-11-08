"""``python -m projektakte`` – start the web server (uvicorn) with the checked configuration."""
from __future__ import annotations

import logging

import uvicorn

from .config import load_settings
from .logsetup import configure_logging
from .startup import ConfigError, trusted_proxies


def main() -> None:
    settings = load_settings()
    configure_logging(getattr(settings, "log_level", "") or "INFO")
    from .main import create_app

    try:
        app = create_app(settings)
    except ConfigError as exc:
        logging.getLogger("projektakte").error("Start abgebrochen: %s", exc)
        raise SystemExit(2) from None
    # proxy_headers: behind a trusted reverse proxy (FORWARDED_ALLOW_IPS) client address and scheme
    # come from X-Forwarded-For/-Proto; log_config=None keeps the logging configured above
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_config=None, log_level="info",
                proxy_headers=True, forwarded_allow_ips=trusted_proxies(settings, warn=True),
                server_header=False)


if __name__ == "__main__":
    main()
