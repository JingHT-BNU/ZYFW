"""REST API 与 WebSocket 路由。"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, WebSocket, WebSocketDisconnect, HTTPException

from .core.config_store import GlobalConfig, PluginConfigStore
from .core import course_cache
from .core.plugin_manager import PluginManager, PluginError, PLUGINS_DIR
from .core.runner import Runner, get_bus

api_router = APIRouter(prefix="/api")

global_config = GlobalConfig()
runner = Runner()
pm = PluginManager(runner)

# ---------------- 窗口关闭自动退出 ----------------
# 页面通过 WebSocket 保持连接;所有客户端断开且宽限期内无人重连(如页面刷新)时,
# 视为 GUI 窗口已关闭,自动停止服务与插件子进程。

GRACE_SECONDS = 8.0
_shutdown_callback = None
_shutdown_timer: threading.Timer | None = None
_ws_count = 0
_ws_lock = threading.Lock()


def set_shutdown_callback(cb) -> None:
    global _shutdown_callback
    _shutdown_callback = cb


def _fire_shutdown() -> None:
    global _shutdown_timer
    with _ws_lock:
        if _ws_count > 0:
            return
        _shutdown_timer = None
        cb = _shutdown_callback
    if cb:
        print("检测到窗口关闭且无客户端连接,自动退出服务", flush=True)
        try:
            cb()
        except Exception:
            pass


def _client_connected() -> None:
    global _ws_count, _shutdown_timer
    with _ws_lock:
        _ws_count += 1
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
            _shutdown_timer = None


def _client_disconnected() -> None:
    global _ws_count, _shutdown_timer
    with _ws_lock:
        _ws_count = max(0, _ws_count - 1)
        if _ws_count == 0 and _shutdown_timer is None and _shutdown_callback is not None:
            _shutdown_timer = threading.Timer(GRACE_SECONDS, _fire_shutdown)
            _shutdown_timer.daemon = True
            _shutdown_timer.start()


def _effective_plugin_config(plugin: dict, cfg: dict) -> dict:
    """合并全局配置:插件账号留空时回退到全局统一身份认证账号。

    合并结果同时写入 account 与顶层 username/password(插件运行时读取后者)。
    注意必须直接赋值而非 setdefault: 配置界面保存的 username/password 键总是
    存在(留空=用全局, 存的是空串), setdefault 遇到空串占位不会覆盖,
    会导致全局账号永远合并不进去。
    """
    g = global_config.get()
    effective = dict(cfg)
    src = effective.get("account")
    if not isinstance(src, dict):
        src = {}
    gsrc = g.get("account")
    if not isinstance(gsrc, dict):
        gsrc = {}
    merged = {
        "username": src.get("username") or gsrc.get("username") or "",
        "password": src.get("password") or gsrc.get("password") or "",
    }
    effective["account"] = merged
    effective["username"] = merged["username"]
    effective["password"] = merged["password"]
    general = effective.get("general")
    if not isinstance(general, dict):
        general = {}
        effective["general"] = general
    global_general = g.get("general")
    if not isinstance(global_general, dict):
        global_general = {}
    for key, value in global_general.items():
        general.setdefault(key, value)
    return effective


def _restore_masked_secrets(patch: object, current: object) -> object:
    """Recursively replace ``******`` placeholders without mutating request data."""
    if isinstance(patch, dict):
        base = current if isinstance(current, dict) else {}
        return {
            key: _restore_masked_secrets(value, base.get(key))
            for key, value in patch.items()
        }
    if isinstance(patch, list):
        base = current if isinstance(current, list) else []
        return [
            _restore_masked_secrets(value, base[i] if i < len(base) else None)
            for i, value in enumerate(patch)
        ]
    if patch == "******" and isinstance(current, str) and current:
        return current
    return patch


def _config_patch(body: dict) -> dict:
    patch = body.get("config")
    if not isinstance(patch, dict):
        raise HTTPException(422, "config 必须是 JSON 对象")
    return patch


def _strip_plugin_styles(html: str) -> str:
    """Remove plugin-provided CSS, including both quote styles for attributes."""
    import re as _re
    html = _re.sub(r"<style\b[^>]*>[\s\S]*?</style>", "", html, flags=_re.I)
    return _re.sub(
        r"""\sstyle\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
        "", html, flags=_re.I,
    )


# ---------------- 插件 ----------------

@api_router.get("/plugins")
def list_plugins():
    return {"plugins": pm.list_plugins()}


@api_router.post("/plugins/install")
async def install_plugin(file: UploadFile = File(...)):
    # 文件名完全由服务端生成, 不拼接客户端可控路径。
    tmp = Path(tempfile.gettempdir()) / f"zyfw-plugin-{uuid.uuid4().hex}.zip"
    try:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(file.file, f)
        result = pm.install(str(tmp))
        get_bus().publish({"type": "plugin_installed", "plugin": result["id"]})
        return result
    except PluginError as e:
        raise HTTPException(400, str(e))
    finally:
        tmp.unlink(missing_ok=True)


@api_router.delete("/plugins/{plugin_id}")
def uninstall_plugin(plugin_id: str):
    try:
        pm.uninstall(plugin_id)
        get_bus().publish({"type": "plugin_uninstalled", "plugin": plugin_id})
        return {"ok": True}
    except PluginError as e:
        raise HTTPException(400, str(e))


@api_router.get("/plugins/{plugin_id}/config")
def get_plugin_config(plugin_id: str):
    try:
        plugin = pm.get_plugin(plugin_id)
    except PluginError as e:
        raise HTTPException(404, str(e))
    cfg = PluginConfigStore(plugin_id).get_masked()
    # 未配置过的字段用清单里的默认值填充,前端表单才有初始显示
    for f in plugin.get("config_schema", []):
        key = f.get("key")
        if key and key not in cfg and "default" in f:
            cfg[key] = f["default"]
    return {"config": cfg, "schema": plugin.get("config_schema", [])}


@api_router.get("/plugins/{plugin_id}/ui")
def get_plugin_ui(plugin_id: str):
    """返回插件自定义配置界面 HTML(config_ui)。

    安全与风格约定: 只允许插件目录内的单个 .html 文件; 强制剥离 <style>
    标签与 style 属性(插件界面不允许自带 CSS, 风格由宿主 zy-* 类统一提供),
    防止插件破坏主界面外观或注入钓鱼样式。脚本保留(经用户主动安装插件)。
    """
    try:
        plugin = pm.get_plugin(plugin_id)
    except PluginError as e:
        raise HTTPException(404, str(e))
    ui_name = plugin.get("config_ui") or ""
    if not ui_name:
        raise HTTPException(404, "该插件未提供自定义配置界面")
    plugin_dir = Path(plugin["dir"]).resolve()
    ui_path = (plugin_dir / ui_name).resolve()
    if not ui_path.is_relative_to(plugin_dir):
        raise HTTPException(400, "配置界面文件不在插件目录内")
    if not ui_path.is_file():
        raise HTTPException(404, f"配置界面文件不存在: {ui_name}")
    html = ui_path.read_text(encoding="utf-8")
    return {"html": _strip_plugin_styles(html)}


@api_router.put("/plugins/{plugin_id}/config")
async def save_plugin_config(plugin_id: str, body: dict):
    try:
        pm.get_plugin(plugin_id)
    except PluginError as e:
        raise HTTPException(404, str(e))
    store = PluginConfigStore(plugin_id)
    old = store.get()
    new = _restore_masked_secrets(_config_patch(body), old)
    store.save(new)
    return {"ok": True}


@api_router.post("/plugins/{plugin_id}/start")
def start_plugin(plugin_id: str):
    try:
        plugin = pm.get_plugin(plugin_id)
    except PluginError as e:
        raise HTTPException(404, str(e))
    if runner.is_running(plugin_id):
        raise HTTPException(409, "插件已在运行")
    cfg = PluginConfigStore(plugin_id).get()
    effective = _effective_plugin_config(plugin, cfg)
    data_dir = pm.plugin_data_dir(plugin_id)
    plugin_dir = Path(plugin["dir"]).resolve()
    entry = (plugin_dir / plugin["entry"]).resolve()
    if not entry.is_relative_to(plugin_dir):
        raise HTTPException(400, "插件入口文件不在插件目录内")
    env_extra = {"PYTHONPATH": str(PLUGINS_DIR)}
    try:
        runner.start(plugin_id, entry, effective, data_dir, env_extra=env_extra)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@api_router.post("/plugins/{plugin_id}/stop")
def stop_plugin(plugin_id: str):
    runner.stop(plugin_id)
    return {"ok": True}


@api_router.get("/plugins/{plugin_id}/logs")
def plugin_logs(plugin_id: str, limit: int = 200):
    limit = max(1, min(limit, 2000))
    return {"events": get_bus().recent(plugin_id, limit)}


# ---------------- 专业目录 / 课程列表(course-grab) ----------------

_GRAB_ID = "course-grab"
_TASK_CATALOG = "catalog"     # 抓取全部专业目录
_TASK_COURSES = "courses"     # 抓取指定专业的课程列表


def _grab_task_running() -> bool:
    """同一时刻只允许一个抓取任务: 目录抓取与课程抓取、插件正常运行互斥。

    Runner 按 plugin_id 管理子进程, 复用 course-grab 的运行槽位天然保证互斥。
    """
    return runner.is_running(_GRAB_ID)


def _start_grab_task(task: str, extra: dict) -> None:
    try:
        plugin = pm.get_plugin(_GRAB_ID)
    except PluginError as e:
        raise HTTPException(404, str(e))
    if _grab_task_running():
        raise HTTPException(409, "抓取任务已在运行")
    cfg = PluginConfigStore(_GRAB_ID).get()
    effective = _effective_plugin_config(plugin, cfg)
    if not effective.get("username") or not effective.get("password"):
        raise HTTPException(400, "请先在全局设置中填写统一身份认证账号")
    effective["task"] = task
    effective.update(extra)
    data_dir = pm.plugin_data_dir(_GRAB_ID)
    plugin_dir = Path(plugin["dir"]).resolve()
    entry = (plugin_dir / plugin["entry"]).resolve()
    if not entry.is_relative_to(plugin_dir):
        raise HTTPException(400, "插件入口文件不在插件目录内")
    env_extra = {"PYTHONPATH": str(PLUGINS_DIR)}
    try:
        runner.start(_GRAB_ID, entry, effective, data_dir, env_extra=env_extra)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@api_router.get("/plugins/{plugin_id}/majors")
def get_majors(plugin_id: str):
    if plugin_id != _GRAB_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    catalog = course_cache.load_catalog()
    if catalog is None:
        raise HTTPException(404, "专业目录尚未抓取, 请先点击「抓取专业目录」")
    return catalog


@api_router.post("/plugins/{plugin_id}/majors/refresh")
def refresh_majors(plugin_id: str):
    if plugin_id != _GRAB_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    _start_grab_task(_TASK_CATALOG, {})
    return {"ok": True}


@api_router.get("/plugins/{plugin_id}/major-courses")
def get_major_courses(
    plugin_id: str,
    level: str = "",
    grade: str = "",
    dept: str = "",
    major: str = "",
):
    if plugin_id != _GRAB_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    if not (level and grade and dept and major):
        raise HTTPException(400, "缺少专业参数")
    data = course_cache.load_courses(level, grade, dept, major)
    if data is None:
        raise HTTPException(404, "该专业课程列表尚未缓存, 请先点击「获取该专业课程列表」")
    return data


@api_router.post("/plugins/{plugin_id}/major-courses/refresh")
def refresh_major_courses(plugin_id: str, body: dict):
    if plugin_id != _GRAB_ID:
        raise HTTPException(404, f"插件不存在: {plugin_id}")
    # 新版配置界面发送 {"specialty": {...}}; 保留顶层旧格式兼容。
    sel = body.get("specialty", body) if isinstance(body, dict) else None
    if not isinstance(sel, dict):
        raise HTTPException(400, "专业参数格式错误")

    def _norm(v) -> str:
        # 兼容 {"v": 值, "t": 文本} 与纯字符串两种形态
        if isinstance(v, dict):
            v = v.get("v", "")
        return str(v or "").strip()

    keys = ("level", "grade", "dept", "major")
    sel = {k: _norm(sel.get(k)) for k in keys}
    missing = [k for k in keys if not sel[k]]
    if missing:
        raise HTTPException(400, "请先选择培养层次、年级、院(系)/部与专业")
    _start_grab_task(_TASK_COURSES, {"specialty": sel})
    return {"ok": True}


# ---------------- 全局配置 ----------------

@api_router.get("/global")
def get_global():
    return {"config": global_config.get_masked()}


@api_router.put("/global")
async def update_global(body: dict):
    patch = _restore_masked_secrets(_config_patch(body), global_config.get())
    global_config.update(patch)
    return {"ok": True}


# ---------------- WebSocket ----------------

@api_router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _client_connected()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _on_event(event: dict):
        loop.call_soon_threadsafe(queue.put_nowait, event)

    unsubscribe = get_bus().subscribe(_on_event)

    async def sender():
        hello = json.dumps({"type": "hello", "history": get_bus().recent(None, 300)},
                           ensure_ascii=False)
        await ws.send_text(hello)
        while True:
            event = await queue.get()
            await ws.send_text(json.dumps(event, ensure_ascii=False))

    async def disconnect_watcher():
        # 前端不发送消息, receive 只会在客户端断开时返回/抛错
        while True:
            await ws.receive_text()

    send_task = asyncio.create_task(sender())
    watch_task = asyncio.create_task(disconnect_watcher())
    try:
        done, _pending = await asyncio.wait(
            {send_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        send_task.cancel()
        watch_task.cancel()
        unsubscribe()
        _client_disconnected()
