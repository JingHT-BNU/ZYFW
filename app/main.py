"""应用入口:启动本地服务并打开 Edge app 窗口。"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import api_router, set_shutdown_callback  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

WEB_DIR = ROOT / "app" / "web"


def setup_consoleless_io() -> None:
    """pythonw 下 stdout/stderr 为 None,任何 print 都会崩;重定向到日志文件。"""
    if sys.stdout is None or sys.stderr is None:
        log_dir = ROOT / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "zyfw-gui.log"
        stream = open(log_file, "a", buffering=1, encoding="utf-8")
        sys.stdout = stream
        sys.stderr = stream


def create_app() -> FastAPI:
    app = FastAPI(title="ZYFW 工具集", docs_url=None, redoc_url=None)
    app.include_router(api_router)
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    # 全站禁用 HTTP 缓存(仅本机运行的小服务, 无带宽顾虑):
    # StaticFiles 只带 ETag/Last-Modified, 浏览器会按启发式策略缓存旧 app.js,
    # 出现"插件版本已是新版、界面仍是旧版"的错位; 而插件抓取用的 Edge 每次都是
    # 全新实例(无缓存)反而正常, 问题只在长期使用的浏览器上暴露。
    # no-store 覆盖静态文件、/ui 界面 HTML 与 API GET 响应, 保证改动即刻生效。
    @app.middleware("http")
    async def _disable_http_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def find_free_port(prefer: int = 8765) -> int:
    for port in (prefer, prefer + 1, prefer + 2):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def open_window_later(url: str) -> None:
    """等服务可访问后再开窗口,避免弹出'拒绝连接'。"""
    if not wait_http(url):
        print(f"服务未能在预期时间内就绪: {url}", flush=True)
        return
    if os.name == "nt":
        for exe in (
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        ):
            if os.path.exists(exe):
                try:
                    subprocess.Popen(
                        [exe, "--app=" + url],
                        creationflags=subprocess.DETACHED_PROCESS,
                    )
                    return
                except OSError:
                    continue
    webbrowser.open(url)


def main() -> None:
    setup_consoleless_io()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-window", action="store_true", help="只启动服务,不开窗口")
    args = parser.parse_args()

    port = args.port or find_free_port()
    app = create_app()

    # 供 start.bat / start.sh 等启动器读取实际端口
    url_file = ROOT / "data" / "zyfw-gui.url"
    url_file.parent.mkdir(parents=True, exist_ok=True)
    url_file.write_text(f"http://127.0.0.1:{port}/", encoding="utf-8")

    # 窗口关闭(所有页面 WebSocket 断开)后自动停止插件并退出服务;
    # --no-window 方式没有窗口, 不注册回调, 服务常驻直到手动停止(与 README 一致)
    if not args.no_window:
        def shutdown() -> None:
            try:
                from app.api import runner
                for pid in list(runner.snapshot().keys()):
                    try:
                        runner.stop(pid)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                url_file.unlink(missing_ok=True)
            except Exception:
                pass
            threading.Timer(1.0, lambda: os._exit(0)).start()

        set_shutdown_callback(shutdown)

    if not args.no_window:
        threading.Thread(
            target=open_window_later, args=(f"http://127.0.0.1:{port}/",), daemon=True
        ).start()

    print(f"ZYFW 工具集已启动: http://127.0.0.1:{port}/", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
