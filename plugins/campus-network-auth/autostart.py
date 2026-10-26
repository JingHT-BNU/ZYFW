"""Install and run campus-network authentication independently of ZYFW."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import platform
import plistlib
import shlex
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

PLUGIN_ID = "campus-network-auth"
THIS_DIR = Path(__file__).resolve().parent
ROOT = THIS_DIR.parents[1]
SERVICE_NAME = "zyfw-campus-network-auth"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))


def _python_command() -> str:
    executable = Path(sys.executable)
    if os.name == "nt":
        windowless = executable.with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return str(executable)


def _launch_command() -> list[str]:
    return [_python_command(), str(THIS_DIR / "autostart.py"), "--run"]


def _run(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0, detail


def _windows_install() -> dict:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    command = subprocess.list2cmdline(_launch_command())
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(
            key, "ZYFW-CampusNetworkAuth", 0, winreg.REG_SZ, command)
    return {"installed": True, "method": "registry-run", "command": command}


def _windows_uninstall() -> dict:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0,
                winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "ZYFW-CampusNetworkAuth")
    except FileNotFoundError:
        pass
    return {"installed": False, "method": "registry-run"}


def _windows_status() -> dict:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _kind = winreg.QueryValueEx(
                key, "ZYFW-CampusNetworkAuth")
        return {
            "installed": True,
            "method": "registry-run",
            "command": value,
        }
    except FileNotFoundError:
        return {"installed": False, "method": "registry-run"}


def _systemd_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / (
        SERVICE_NAME + ".service")


def _linux_install() -> dict:
    path = _systemd_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    command = " ".join(shlex.quote(part) for part in _launch_command())
    path.write_text(
        "[Unit]\n"
        "Description=ZYFW campus network authentication\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={command}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    ok, detail = _run(["systemctl", "--user", "daemon-reload"])
    if not ok:
        raise RuntimeError(f"systemctl daemon-reload 失败: {detail}")
    ok, detail = _run([
        "systemctl", "--user", "enable", SERVICE_NAME + ".service"])
    if not ok:
        raise RuntimeError(f"启用 systemd 用户服务失败: {detail}")
    linger_ok, linger_detail = _run([
        "loginctl", "enable-linger", getpass.getuser()])
    return {
        "installed": True,
        "method": "systemd-user",
        "boot_without_login": linger_ok,
        "warning": "" if linger_ok else linger_detail,
    }


def _linux_uninstall() -> dict:
    _run(["systemctl", "--user", "disable", "--now",
          SERVICE_NAME + ".service"])
    _systemd_path().unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])
    return {"installed": False, "method": "systemd-user"}


def _linux_status() -> dict:
    path = _systemd_path()
    enabled, _detail = _run([
        "systemctl", "--user", "is-enabled", SERVICE_NAME + ".service"])
    return {
        "installed": path.is_file() and enabled,
        "method": "systemd-user",
        "path": str(path),
    }


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (
        "com.zyfw.campus-network-auth.plist")


def _macos_install() -> dict:
    path = _launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": "com.zyfw.campus-network-auth",
        "ProgramArguments": _launch_command(),
        "RunAtLoad": True,
        "KeepAlive": False,
    }
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", domain, str(path)])
    ok, detail = _run(["launchctl", "bootstrap", domain, str(path)])
    if not ok:
        raise RuntimeError(f"加载 LaunchAgent 失败: {detail}")
    return {
        "installed": True,
        "method": "launch-agent",
        "path": str(path),
    }


def _macos_uninstall() -> dict:
    path = _launch_agent_path()
    _run([
        "launchctl", "bootout", f"gui/{os.getuid()}", str(path)])
    path.unlink(missing_ok=True)
    return {"installed": False, "method": "launch-agent"}


def _macos_status() -> dict:
    path = _launch_agent_path()
    return {
        "installed": path.is_file(),
        "method": "launch-agent",
        "path": str(path),
    }


def install() -> dict:
    system = platform.system().lower()
    if system == "windows":
        return _windows_install()
    if system == "linux":
        return _linux_install()
    if system == "darwin":
        return _macos_install()
    raise RuntimeError(f"不支持的开机自启平台: {platform.system()}")


def uninstall() -> dict:
    system = platform.system().lower()
    if system == "windows":
        return _windows_uninstall()
    if system == "linux":
        return _linux_uninstall()
    if system == "darwin":
        return _macos_uninstall()
    raise RuntimeError(f"不支持的开机自启平台: {platform.system()}")


def status() -> dict:
    system = platform.system().lower()
    if system == "windows":
        return _windows_status()
    if system == "linux":
        return _linux_status()
    if system == "darwin":
        return _macos_status()
    return {
        "installed": False,
        "method": "unsupported",
        "error": platform.system(),
    }


def apply_enabled(enabled: bool) -> dict:
    return install() if enabled else uninstall()


class _InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.handle.read(1) == b"":
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError(
                "已有校园网认证自启进程在运行") from exc
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close()


def _load_config() -> dict:
    from app.core.config_store import PluginConfigStore
    return PluginConfigStore(PLUGIN_ID).get()


def run_autostart() -> int:
    data_dir = ROOT / "data" / "plugins" / PLUGIN_ID
    data_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("campus-network-auth-autostart")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        data_dir / "autostart.log",
        maxBytes=1_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        with _InstanceLock(data_dir / "autostart.lock"):
            config = _load_config()
            if not config.get("autostart"):
                logger.info("autostart disabled; exiting")
                return 0
            from srun_core import connect_with_retries
            result = connect_with_retries(
                config, log=lambda message: logger.info(message))
            logger.info("result: %s", json.dumps(result, ensure_ascii=False))
            return 0
    except Exception as exc:
        logger.exception("campus network autostart failed: %s", exc)
        return 1
    finally:
        logger.removeHandler(handler)
        handler.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--install", action="store_true", help="Install user autostart")
    parser.add_argument(
        "--uninstall", action="store_true", help="Remove user autostart")
    parser.add_argument(
        "--status", action="store_true", help="Print autostart status")
    parser.add_argument(
        "--run", action="store_true", help="Run one autostart attempt")
    args = parser.parse_args()
    try:
        if args.install:
            print(json.dumps(install(), ensure_ascii=False))
        elif args.uninstall:
            print(json.dumps(uninstall(), ensure_ascii=False))
        elif args.status:
            print(json.dumps(status(), ensure_ascii=False))
        elif args.run:
            return run_autostart()
        else:
            parser.error("请指定 --install、--uninstall、--status 或 --run")
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "status": status(),
        }, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
