"""Cross-platform detection of active Ethernet and Wi-Fi interfaces."""
from __future__ import annotations

import json
import platform
import re
import subprocess
from pathlib import Path


def _run(command: list[str], timeout: float = 10.0) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return result.stdout or ""


def _classify(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in (
            "802.11", "wi-fi", "wifi", "wireless", "wlan", "无线", "wi-fi")):
        return "wifi"
    if any(marker in lowered for marker in (
            "802.3", "ethernet", "以太网", "乙太網路", "local area connection")):
        return "ethernet"
    return ""


def _windows() -> dict:
    interfaces: list[dict] = []
    script = (
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "Get-NetAdapter -Physical -ErrorAction SilentlyContinue | "
        "Where-Object {$_.Status -eq 'Up'} | "
        "Select-Object Name,InterfaceDescription,PhysicalMediaType | "
        "ConvertTo-Json -Compress"
    )
    try:
        output = _run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script
        ]).strip()
        if output:
            value = json.loads(output)
            rows = value if isinstance(value, list) else [value]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                text = " ".join(str(row.get(key) or "") for key in (
                    "Name", "InterfaceDescription", "PhysicalMediaType"))
                kind = _classify(text)
                if kind:
                    interfaces.append({
                        "name": str(row.get("Name") or ""),
                        "kind": kind,
                    })
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    if not interfaces:
        try:
            output = _run(["ipconfig", "/all"])
            blocks = re.split(r"\r?\n(?=\S)", output)
            for block in blocks:
                lowered = block.lower()
                if "media disconnected" in lowered or "未连接" in block:
                    continue
                kind = _classify(block.splitlines()[0] if block else "")
                if kind:
                    interfaces.append({"name": "", "kind": kind})
        except (OSError, subprocess.TimeoutExpired):
            pass
    return _summary(interfaces)


def _linux() -> dict:
    interfaces: list[dict] = []
    root = Path("/sys/class/net")
    if root.is_dir():
        for interface in root.iterdir():
            if interface.name == "lo":
                continue
            try:
                state = (interface / "operstate").read_text(
                    encoding="utf-8").strip().lower()
                carrier = (interface / "carrier")
                active = state in {"up", "unknown"} or (
                    carrier.exists()
                    and carrier.read_text(encoding="utf-8").strip() == "1")
            except OSError:
                continue
            if not active:
                continue
            kind = "wifi" if (interface / "wireless").exists() else "ethernet"
            interfaces.append({"name": interface.name, "kind": kind})
    return _summary(interfaces)


def _macos() -> dict:
    interfaces: list[dict] = []
    try:
        output = _run(["networksetup", "-listallhardwareports"])
    except (OSError, subprocess.TimeoutExpired):
        return _summary(interfaces)
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("Hardware Port:"):
            continue
        name = line.split(":", 1)[1].strip()
        device = ""
        if index + 1 < len(lines) and lines[index + 1].startswith("Device:"):
            device = lines[index + 1].split(":", 1)[1].strip()
        if not device:
            continue
        kind = _classify(name)
        if not kind:
            continue
        try:
            status = _run(["ifconfig", device])
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "status: active" in status.lower():
            interfaces.append({"name": device, "kind": kind})
    return _summary(interfaces)


def _summary(interfaces: list[dict]) -> dict:
    return {
        "ethernet": any(item["kind"] == "ethernet" for item in interfaces),
        "wifi": any(item["kind"] == "wifi" for item in interfaces),
        "interfaces": interfaces,
    }


def detect_connection() -> dict:
    system = platform.system().lower()
    if system == "windows":
        return _windows()
    if system == "linux":
        return _linux()
    if system == "darwin":
        return _macos()
    return {
        "ethernet": False,
        "wifi": False,
        "interfaces": [],
        "error": f"Unsupported OS: {platform.system()}",
    }


def select_portal(config: dict) -> tuple[str, str]:
    wired = str(config.get("wired_portal") or "").strip()
    wireless = str(config.get("wireless_portal") or "").strip()
    mode = str(config.get("connection_mode") or "auto").strip().lower()
    detected = detect_connection()
    if mode == "wired":
        return wired or wireless, "wired"
    if mode == "wireless":
        return wireless or wired, "wireless"
    if detected.get("ethernet") and wired:
        return wired, "wired"
    if detected.get("wifi") and wireless:
        return wireless, "wireless"
    if wired:
        return wired, "wired"
    if wireless:
        return wireless, "wireless"
    return "", "unknown"
