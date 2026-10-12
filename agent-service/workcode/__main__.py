"""启动入口：python -m workcode"""

from __future__ import annotations

import uvicorn

from .config import load_settings
from .server import app


def main() -> None:
    settings = load_settings()
    print(f"WorkCode agent service → ws://{settings.host}:{settings.port}/ws (mock={settings.mock})")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
