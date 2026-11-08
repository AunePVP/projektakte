import logging

import uvicorn

from .config import load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    uvicorn.run("projektakte.main:create_app", factory=True, host="0.0.0.0", port=settings.port,
                log_level="info")


if __name__ == "__main__":
    main()
