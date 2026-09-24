from __future__ import annotations

import asyncio
import os
import sys

import uvicorn


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    uvicorn.run(
        "api.main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        reload=False,
    )
