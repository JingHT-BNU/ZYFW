"""全局与插件配置的持久化存储,凭据字段使用 Windows DPAPI 加密。"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = _ROOT / "data"

_SECRET_KEYS = {"password", "secret", "token"}
_DPAPI_PREFIX = "dpapi:"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_PATH_LOCKS: dict[Path, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    """Share one lock across JsonStore instances targeting the same file."""
    key = path.resolve()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


_crypt32 = ctypes.windll.crypt32
_kernel32 = ctypes.windll.kernel32

_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
]
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPCWSTR),
    ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL


def _make_blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_protect(plaintext: str) -> str:
    in_blob = _make_blob(plaintext.encode("utf-8"))
    out_blob = _DATA_BLOB()
    if not _crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    ):
        raise OSError("CryptProtectData failed")
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _kernel32.LocalFree(out_blob.pbData)
    return _DPAPI_PREFIX + base64.b64encode(raw).decode("ascii")


def dpapi_unprotect(token: str) -> str:
    if not token.startswith(_DPAPI_PREFIX):
        return token
    raw = base64.b64decode(token[len(_DPAPI_PREFIX):])
    in_blob = _make_blob(raw)
    out_blob = _DATA_BLOB()
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        out = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _kernel32.LocalFree(out_blob.pbData)
    return out.decode("utf-8")


def _encrypt_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (dpapi_protect(v) if k in _SECRET_KEYS and isinstance(v, str) and v and not v.startswith(_DPAPI_PREFIX)
                else _encrypt_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_encrypt_secrets(i) for i in obj]
    return obj


def _decrypt_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _SECRET_KEYS and isinstance(v, str) and v.startswith(_DPAPI_PREFIX):
                # 密文由旧 Windows 环境(用户配置重建/系统密码重置等)加密时当前
                # 用户永远解不开;此时清空该字段比让整个配置接口 500 更合理,
                # 用户在设置页重新输入即可。保存时空值不会触发再加密。
                try:
                    out[k] = dpapi_unprotect(v)
                except (OSError, ValueError, UnicodeDecodeError):
                    out[k] = ""
            else:
                out[k] = _decrypt_secrets(v)
        return out
    if isinstance(obj, list):
        return [_decrypt_secrets(i) for i in obj]
    return obj


def mask_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("******" if k in _SECRET_KEYS and isinstance(v, str) and v else mask_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask_secrets(i) for i in obj]
    return obj


class JsonStore:
    """带原子写入与线程锁的 JSON 文件存储。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _path_lock(path)

    def _read_raw(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("配置根节点必须是 JSON 对象")
            return data
        except (json.JSONDecodeError, OSError, ValueError):
            backup = self.path.with_suffix(".corrupt.json")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return {}

    def _write_raw(self, data: dict) -> None:
        # 同进程内多个 PluginConfigStore 实例可能并发保存同一路径。唯一临时
        # 文件名可避免它们互相覆盖尚未替换的临时文件。
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as f:
                tmp_path = f.name
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
            tmp_path = None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def load(self) -> dict:
        with self._lock:
            return _decrypt_secrets(self._read_raw())

    def save(self, data: dict) -> None:
        with self._lock:
            self._write_raw(_encrypt_secrets(data))

    def update(self, patch: dict) -> dict:
        with self._lock:
            current = _decrypt_secrets(self._read_raw())
            _deep_update(current, patch)
            self._write_raw(_encrypt_secrets(current))
            return current


def _deep_update(base: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


GLOBAL_DEFAULTS: dict = {
    "account": {"username": "", "password": ""},
    "general": {
        "driver_path": "",
        "headless_default": True,
        "spoc_workers": 8,
        "default_speedup": 3.0,
        "cookie_life_days": 7,
    },
}


def _migrate_legacy_accounts(data: dict) -> dict:
    """旧版双账号(zyfw_account/spoc_account)合并为统一身份认证 account。"""
    if "account" in data:
        if not isinstance(data["account"], dict):
            data["account"] = {}
        return data
    zyfw = data.pop("zyfw_account", {}) or {}
    spoc = data.pop("spoc_account", {}) or {}
    merged = {
        "username": zyfw.get("username") or spoc.get("username") or "",
        "password": zyfw.get("password") or spoc.get("password") or "",
    }
    if isinstance(merged["password"], str) and merged["password"].startswith("dpapi:"):
        pass  # 密文保持原样,save 时不再重复加密前缀判断已处理
    new_data = dict(data)
    new_data["account"] = merged
    return new_data


class GlobalConfig:
    def __init__(self, store: JsonStore | None = None):
        self.store = store or JsonStore(DATA_DIR / "global.json")
        if not self.store.path.exists():
            self.store.save(GLOBAL_DEFAULTS)

    def get(self) -> dict:
        data = self.store.load()
        data = _migrate_legacy_accounts(data)
        if not isinstance(data.get("general"), dict):
            data["general"] = {}
        merged = json.loads(json.dumps(GLOBAL_DEFAULTS))
        _deep_update(merged, data)
        return merged

    def update(self, patch: dict) -> dict:
        return self.store.update(patch)

    def get_masked(self) -> dict:
        return mask_secrets(self.get())


class PluginConfigStore:
    def __init__(self, plugin_id: str):
        self.store = JsonStore(DATA_DIR / "plugins" / plugin_id / "config.json")

    def get(self) -> dict:
        return self.store.load()

    def save(self, data: dict) -> None:
        self.store.save(data)

    def get_masked(self) -> dict:
        return mask_secrets(self.get())
