"""Entry point: `burnrate` or `python -m burnrate`."""

from __future__ import annotations

import logging

import uvicorn

from .app import create_app
from .config import Config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = Config.from_env()
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        access_log=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
