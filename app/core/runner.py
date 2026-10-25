"""插件子进程生命周期管理与全局事件广播。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parents[2]
PYTHON_EXE = sys.executable

STATUS_STOPPED = "stopped"
STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_EXITED = "exited"
STATUS_CRASHED = "crashed"
STATUS_STOPPING = "stopping"


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


class EventBus:
    """进程内事件广播:WebSocket 订阅者 + 环形日志缓冲(供前端回看)。"""

    def __init__(self, history: int = 2000):
        self._subs: set[Callable[[dict], None]] = set()
        self._lock = threading.Lock()
        self._history: deque[dict] = deque(maxlen=history)

    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.add(callback)

        def _unsub():
            with self._lock:
                self._subs.discard(callback)

        return _unsub

    def publish(self, event: dict) -> None:
        with self._lock:
            self._history.append(event)
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(event)
            except Exception:
                pass

    def recent(self, plugin_id: str | None = None, limit: int = 200) -> list[dict]:
        with self._lock:
            events = list(self._history)
        if plugin_id is not None:
            events = [e for e in events if e.get("plugin") == plugin_id]
        return events[-limit:]


_event_bus: EventBus | None = None
_event_bus_lock = threading.Lock()


def get_bus() -> EventBus:
    global _event_bus
    if _event_bus is None:
        with _event_bus_lock:
            if _event_bus is None:
                _event_bus = EventBus()
    return _event_bus


class PluginProcess:
    def __init__(self, plugin_id: str, proc: subprocess.Popen, config_path: Path):
        self.plugin_id = plugin_id
        self.proc = proc
        self.config_path = config_path
        self.started_at = time.time()
        self.status = STATUS_RUNNING
        self.exit_code: int | None = None
        # 最近一次 progress 事件(供 /api/plugins 轮询, 供不走 WebSocket 的
        # 场景如自定义配置界面读取); 进程结束后清除避免陈旧进度
        self.last_progress: dict | None = None

    def stop(self, timeout: float = 8.0) -> bool:
        """先发送温和停止信号,超时后结束整棵进程树。返回是否仍在运行。"""
        if self.proc.poll() is not None:
            return False
        self.status = STATUS_STOPPING
        try:
            if os.name == "nt":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_tree()
        return self.proc.poll() is None

    def _kill_tree(self) -> None:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                    capture_output=True, timeout=10, check=False,
                )
            else:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class Runner:
    """以子进程运行插件,采集 stdout/stderr 行并转换为事件。"""

    def __init__(self, bus: EventBus | None = None):
        self.bus = bus or get_bus()
        self._processes: dict[str, PluginProcess] = {}
        self._lock = threading.Lock()

    # ---------- 查询 ----------

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            out = {}
            for pid_name, pp in self._processes.items():
                alive = pp.proc.poll() is None
                if not alive and pp.status == STATUS_RUNNING:
                    pp.status = STATUS_EXITED
                    pp.exit_code = pp.proc.returncode
                elif not alive and pp.status == STATUS_STOPPING:
                    pp.status = STATUS_STOPPED
                    pp.exit_code = pp.proc.returncode
                running = alive and pp.status in (
                    STATUS_STARTING, STATUS_RUNNING, STATUS_STOPPING)
                out[pid_name] = {
                    "running": running,
                    "status": pp.status,
                    "pid": pp.proc.pid if running else None,
                    "started_at": pp.started_at,
                    "progress": dict(pp.last_progress) if pp.last_progress else None,
                }
            return out

    def is_running(self, plugin_id: str) -> bool:
        return self.snapshot().get(plugin_id, {}).get("running", False)

    # ---------- 生命周期 ----------

    def start(
        self,
        plugin_id: str,
        entry_path: Path,
        config: dict,
        data_dir: Path,
        env_extra: dict[str, str] | None = None,
    ) -> None:
        with self._lock:
            existing = self._processes.get(plugin_id)
            if existing and existing.proc.poll() is None:
                raise RuntimeError(f"插件 {plugin_id} 已在运行")
            # 运行时配置含解密后的凭据, 使用唯一隐藏文件并在进程结束后删除。
            _safe_unlink(data_dir / "runtime_config.json")
            for stale in data_dir.glob(".runtime-*.json"):
                _safe_unlink(stale)
            config_path = data_dir / f".runtime-{uuid.uuid4().hex}.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            if env_extra:
                env.update(env_extra)

            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            popen_extra = {} if os.name == "nt" else {"start_new_session": True}

            try:
                proc = subprocess.Popen(
                    [
                        PYTHON_EXE, str(entry_path),
                        "--config", str(config_path),
                        "--datadir", str(data_dir),
                    ],
                    cwd=str(entry_path.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    creationflags=creationflags,
                    **popen_extra,
                )
            except Exception:
                _safe_unlink(config_path)
                raise
            pp = PluginProcess(plugin_id, proc, config_path)
            self._processes[plugin_id] = pp

        # 启动标记必须先于首条子进程输出发布, 否则自定义界面按日志定位
        # 本次任务起点时会漏掉启动早期的 progress。
        self.bus.publish({
            "type": "plugin_status", "plugin": plugin_id,
            "status": STATUS_RUNNING, "pid": proc.pid,
        })
        t = threading.Thread(target=self._pump, args=(pp,), daemon=True, name=f"pump-{plugin_id}")
        t.start()

    def stop(self, plugin_id: str) -> None:
        with self._lock:
            pp = self._processes.get(plugin_id)
        if not pp:
            return
        # 终态统一由 _pump 发布, 避免 exited/stopped 重复或顺序倒置。
        pp.stop()

    # ---------- 输出采集 ----------

    def _pump(self, pp: PluginProcess) -> None:
        proc = pp.proc
        assert proc.stdout is not None
        for raw_line in iter(proc.stdout.readline, ""):
            line = raw_line.rstrip("\r\n")
            if not line:
                continue
            event = self._parse_line(pp.plugin_id, line)
            if event.get("type") == "progress":
                # 记录最近一次进度(过滤事件路由字段, 附加接收时间戳)
                pp.last_progress = {
                    k: v for k, v in event.items()
                    if k not in ("type", "plugin", "__event__")
                }
                pp.last_progress["ts"] = round(time.time(), 3)
            self.bus.publish(event)
        code = proc.wait()
        _safe_unlink(pp.config_path)
        with self._lock:
            if pp.status not in (STATUS_STOPPING,):
                pp.status = STATUS_CRASHED if code not in (0, None) else STATUS_EXITED
            else:
                pp.status = STATUS_STOPPED
            pp.exit_code = code
            pp.last_progress = None   # 进程已结束, 清除避免陈旧进度
        self.bus.publish({
            "type": "plugin_status", "plugin": pp.plugin_id,
            "status": pp.status, "exit_code": code,
        })

    @staticmethod
    def _parse_line(plugin_id: str, line: str) -> dict:
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}") and "__event__" in stripped:
            try:
                data = json.loads(stripped)
                if isinstance(data, dict) and "__event__" in data:
                    event = {**data, "plugin": plugin_id}
                    event["type"] = str(data["__event__"])
                    return event
            except json.JSONDecodeError:
                pass
        return {"type": "log", "plugin": plugin_id, "line": line, "ts": time.time()}
