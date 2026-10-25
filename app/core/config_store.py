"""全局与插件配置的持久化存储与跨平台凭据加密。"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from getpass import getuser
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = _ROOT / "data"

_SECRET_KEYS = {"password", "secret", "token"}
_FERNET_PREFIX = "fernet:"
_FERNET_KEY_NAME = ".credentials.key"
_RETIRED_SECRET_PREFIX = "dpapi:"
_PATH_LOCKS: dict[Path, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()
_FERNET_KEY_LOCK = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    """Share one lock across JsonStore instances targeting the same file."""
    key = path.resolve()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _fernet_key_path() -> Path:
    return DATA_DIR / _FERNET_KEY_NAME


def _secure_key_file(path: Path) -> None:
    if os.name == "nt":
        user = os.environ.get("USERNAME") or getuser()
        subprocess.run(
            [
                "icacls", str(path), "/inheritance:r",
                "/grant:r", f"{user}:(R,W)",
            ],
            capture_output=True,
            check=True,
            timeout=10,
        )
    else:
        os.chmod(path, 0o600)


def _load_fernet_key() -> bytes:
    path = _fernet_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _FERNET_KEY_LOCK:
        if path.exists():
            key = path.read_bytes().strip()
            _secure_key_file(path)
            if not key:
                raise OSError("凭据密钥文件为空")
            return key

        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = path.read_bytes().strip()
            _secure_key_file(path)
            if not key:
                raise OSError("凭据密钥文件为空")
            return key
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        _secure_key_file(path)
        return key


def fernet_protect(plaintext: str) -> str:
    from cryptography.fernet import Fernet

    token = Fernet(_load_fernet_key()).encrypt(plaintext.encode("utf-8"))
    return _FERNET_PREFIX + token.decode("ascii")


def fernet_unprotect(token: str) -> str:
    if not token.startswith(_FERNET_PREFIX):
        return token
    from cryptography.fernet import Fernet, InvalidToken

    raw = token[len(_FERNET_PREFIX):].encode("ascii")
    try:
        return Fernet(_load_fernet_key()).decrypt(raw).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("凭据密钥与密文不匹配") from e


def protect_secret(plaintext: str) -> str:
    return fernet_protect(plaintext)


def decrypt_secret(token: str) -> str:
    if token.startswith(_FERNET_PREFIX):
        return fernet_unprotect(token)
    return token


def _encrypt_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (protect_secret(v) if k in _SECRET_KEYS and isinstance(v, str)
                and v and not v.startswith(_FERNET_PREFIX)
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
            if k in _SECRET_KEYS and isinstance(v, str):
                if v.startswith(_RETIRED_SECRET_PREFIX):
                    out[k] = ""
                    continue
                if not v.startswith(_FERNET_PREFIX):
                    out[k] = v
                    continue
                # 无法解密时清空单个字段, 保留其他配置并允许用户重新输入。
                try:
                    out[k] = decrypt_secret(v)
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
        "browser": "edge",
        "driver_path": "",
        "geckodriver_path": "",
        "chromedriver_path": "",
        "chromium_path": "",
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
