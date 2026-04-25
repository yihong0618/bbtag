#!/usr/bin/env python3
"""
启动 BluETag server 并挂载本地 Web UI。

用法:
    uv run examples/run_web_ui.py

需要先在项目根目录的 .env 配置 BLUETAG_API_TOKEN。
打开 http://127.0.0.1:8090/，把 token 填到右上角输入框即可。
"""

from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles

from bluetag.server import app, settings

WEB_DIR = Path(__file__).parent / "web_ui"
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port)
