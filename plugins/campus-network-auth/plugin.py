"""Campus network authentication plugin."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _sdk.sdk import PluginContext  # noqa: E402

from srun_core import SrunError, connect_with_retries  # noqa: E402


def main() -> None:
    ctx = PluginContext()
    config = {
        "username": str(ctx.cfg("username") or "").strip(),
        "password": str(ctx.cfg("password") or ""),
        "wired_portal": ctx.cfg("wired_portal", ""),
        "wireless_portal": ctx.cfg("wireless_portal", ""),
        "connection_mode": ctx.cfg("connection_mode", "auto"),
        "ac_id": ctx.cfg("ac_id", "1"),
        "login_type": ctx.cfg("login_type", "1"),
        "ip": ctx.cfg("ip", ""),
        "request_timeout": ctx.cfg("request_timeout", 10),
        "retry_interval": ctx.cfg("retry_interval", 10),
        "max_attempts": ctx.cfg("max_attempts", 3),
    }
    try:
        ctx.status("正在检测校园网连接…")
        result = connect_with_retries(
            config,
            log=ctx.log,
            status=ctx.status,
        )
        ctx.progress(
            done=1,
            total=1,
            portal=result.get("portal", ""),
            connection=result.get("connection", ""),
        )
        ctx.status(result["message"], done=True)
    except (SrunError, RuntimeError, ValueError) as exc:
        ctx.log(f"校园网认证失败: {exc}")
        ctx.progress(done=0, total=1)
        ctx.status(f"校园网认证失败: {exc}", error=True)


if __name__ == "__main__":
    main()
