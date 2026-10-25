"""插件发现、安装、卸载与清单校验。"""
from __future__ import annotations

import json
import shutil
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = _ROOT / "plugins"
USER_PLUGINS_DIR = PLUGINS_DIR / "installed"
ALLOWED_ENTRY = {"plugin.py"}
MAX_ZIP_MEMBERS = 500
MAX_ZIP_UNCOMPRESSED_BYTES = 50 * 1024 * 1024

VALID_TYPES = {"string", "password", "number", "bool", "choice", "string-list", "course-table", "specialty"}
BUILTIN_IDS = {"course-grab", "spoc-autoplay"}


class PluginError(Exception):
    pass


def _validate_plugin_id(plugin_id: object) -> str:
    if (not isinstance(plugin_id, str) or not plugin_id or len(plugin_id) > 64
            or not all(c.isalnum() or c in "-_" for c in plugin_id)):
        raise PluginError(f"非法插件 id: {plugin_id!r}")
    return plugin_id


def _validate_manifest(manifest: Any, source: str) -> dict:
    if not isinstance(manifest, dict):
        raise PluginError(f"{source}: plugin.json 必须是 JSON 对象")
    for key in ("id", "name", "version", "entry"):
        if not manifest.get(key):
            raise PluginError(f"{source}: plugin.json 缺少必填字段 {key}")
    try:
        _validate_plugin_id(manifest["id"])
    except PluginError as e:
        raise PluginError(f"{source}: {e}") from e
    if Path(manifest["entry"]).name != manifest["entry"] or Path(manifest["entry"]).suffix != ".py":
        raise entry_err(source)
    schema = manifest.get("config_schema", [])
    if not isinstance(schema, list):
        raise PluginError(f"{source}: config_schema 必须是数组")
    for item in schema:
        if not isinstance(item, dict) or "key" not in item or "label" not in item:
            raise PluginError(f"{source}: config_schema 项需要 key/label")
        if item.get("type", "string") not in VALID_TYPES:
            raise PluginError(f"{source}: 不支持的字段类型 {item.get('type')!r}")
    # 可选的自定义配置界面: 插件目录内单文件 .html(不含 CSS, 风格由宿主统一提供)
    ui = manifest.get("config_ui")
    if ui is not None:
        if not isinstance(ui, str) or Path(ui).name != ui or Path(ui).suffix != ".html":
            raise PluginError(f"{source}: config_ui 必须是插件目录内的单文件名 .html")
    return manifest


def entry_err(source: str) -> PluginError:
    return PluginError(f"{source}: entry 必须是插件目录内的 plugin.py 之类单文件名")


_USER_PLUGINS_READY = False


def _ensure_user_plugins_dir() -> None:
    global _USER_PLUGINS_READY
    if not _USER_PLUGINS_READY:
        USER_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        _USER_PLUGINS_READY = True


class PluginManager:
    def __init__(self, runner):
        self._runner = runner  # app.core.runner.Runner, 用于运行态查询
        self._lock = threading.Lock()

    # ---------- 枚举 ----------

    def list_plugins(self) -> list[dict]:
        _ensure_user_plugins_dir()
        with self._lock:
            plugins: dict[str, dict] = {}
            for d in sorted(PLUGINS_DIR.iterdir()):
                if not d.is_dir() or d.name.startswith("_"):
                    continue
                manifest = self._load_manifest(d)
                if not manifest:
                    continue
                plugins[manifest["id"]] = {
                    **manifest,
                    "builtin": manifest["id"] in BUILTIN_IDS,
                    "dir": str(d),
                }
            for d in sorted(USER_PLUGINS_DIR.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                manifest = self._load_manifest(d)
                if not manifest:
                    continue
                if manifest["id"] in plugins:
                    raise PluginError(f"插件 id 冲突: {manifest['id']}")
                plugins[manifest["id"]] = {
                    **manifest,
                    "builtin": False,
                    "dir": str(d),
                }
        result = []
        runner_state = self._runner.snapshot()
        for p in plugins.values():
            st = runner_state.get(p["id"], {})
            result.append({
                "id": p["id"],
                "name": p["name"],
                "version": p["version"],
                "description": p.get("description", ""),
                "builtin": p["builtin"],
                "config_schema": p.get("config_schema", []),
                "config_ui": p.get("config_ui", ""),
                "dir": p["dir"],
                "entry": p["entry"],
                "running": st.get("running", False),
                "status": st.get("status", "stopped"),
                "pid": st.get("pid"),
                "progress": st.get("progress"),
            })
        result.sort(key=lambda x: (not x["builtin"], x["id"]))
        return result

    def get_plugin(self, plugin_id: str) -> dict:
        for p in self.list_plugins():
            if p["id"] == plugin_id:
                return p
        raise PluginError(f"插件不存在: {plugin_id}")

    def _load_manifest(self, plugin_dir: Path) -> dict | None:
        manifest_path = plugin_dir / "plugin.json"
        if not manifest_path.exists():
            return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            return _validate_manifest(manifest, plugin_dir.name)
        except (json.JSONDecodeError, PluginError, OSError):
            return None

    # ---------- 安装 ----------

    def install(self, zip_path: str) -> dict:
        _ensure_user_plugins_dir()
        with self._lock:
            work = PLUGINS_DIR / f".tmp-install-{uuid.uuid4().hex[:8]}"
            work.mkdir(parents=True, exist_ok=True)
            try:
                try:
                    with zipfile.ZipFile(zip_path) as zf:
                        infos = zf.infolist()
                        if len(infos) > MAX_ZIP_MEMBERS:
                            raise PluginError(f"插件包文件过多(上限 {MAX_ZIP_MEMBERS})")
                        total_size = sum(info.file_size for info in infos)
                        if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
                            raise PluginError("插件包解压后过大(上限 50 MB)")
                        for info in infos:
                            _check_zip_member(info.filename)
                        zf.extractall(work)
                    # 兼容 plugin.json 位于根目录或单层目录内两种 zip 结构。
                    if (work / "plugin.json").exists():
                        src = work
                    else:
                        dirs_with_manifest = [
                            e for e in work.iterdir()
                            if e.is_dir() and (e / "plugin.json").exists()
                        ]
                        if not dirs_with_manifest:
                            raise PluginError("zip 中未找到 plugin.json")
                        if len(dirs_with_manifest) > 1:
                            raise PluginError("zip 中发现多个 plugin.json,无法确定插件根目录")
                        src = dirs_with_manifest[0]
                    manifest = self._load_manifest(src)
                    if not manifest:
                        raise PluginError("plugin.json 校验失败")
                    if manifest["id"] in BUILTIN_IDS:
                        raise PluginError(f"插件 id {manifest['id']} 与内置插件冲突")
                    dest = USER_PLUGINS_DIR / manifest["id"]
                    if dest.exists() or (PLUGINS_DIR / manifest["id"]).exists():
                        raise PluginError(f"插件 {manifest['id']} 已安装,请先卸载或直接更新配置")
                    shutil.move(str(src), str(dest))
                    return {"id": manifest["id"], "name": manifest["name"], "version": manifest["version"]}
                except zipfile.BadZipFile:
                    raise PluginError("不是有效的 zip 文件")
            finally:
                shutil.rmtree(work, ignore_errors=True)

    def uninstall(self, plugin_id: str) -> None:
        _ensure_user_plugins_dir()
        with self._lock:
            _validate_plugin_id(plugin_id)
            if plugin_id in BUILTIN_IDS:
                raise PluginError("内置插件不可删除")
            d = USER_PLUGINS_DIR / plugin_id
            if self._runner.is_running(plugin_id):
                raise PluginError("插件正在运行,请先停止")
            if not d.exists():
                raise PluginError(f"插件不存在: {plugin_id}")
            shutil.rmtree(d)

    def plugin_data_dir(self, plugin_id: str) -> Path:
        _validate_plugin_id(plugin_id)
        d = DATA_DIR_ROOT / plugin_id
        d.mkdir(parents=True, exist_ok=True)
        return d


DATA_DIR_ROOT = _ROOT / "data" / "plugins"


def _check_zip_member(name: str) -> None:
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        raise PluginError(f"zip 内含非法路径: {name}")
