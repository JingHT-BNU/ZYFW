"""后端核心逻辑自测(不启动服务)。"""
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config_store import (  # noqa: E402
    GlobalConfig, JsonStore, dpapi_protect, dpapi_unprotect,
)
from app.core.plugin_manager import PluginManager, PluginError  # noqa: E402
import app.core.plugin_manager as plugin_manager_module  # noqa: E402
from app.core.runner import Runner  # noqa: E402


def main() -> None:
    token = dpapi_protect("hello-密码123")
    assert token.startswith("dpapi:"), token[:10]
    assert dpapi_unprotect(token) == "hello-密码123"
    print("[ok] DPAPI encrypt/decrypt roundtrip")

    # 所有写入都落在临时目录, 不得触碰真实 data/global.json。
    with tempfile.TemporaryDirectory(prefix="zyfw-selftest-") as td:
        g = GlobalConfig(JsonStore(Path(td) / "global.json"))
        g.update({"account": {"username": "u1", "password": "p1"}})
        masked = g.get_masked()
        assert masked["account"]["password"] == "******"
        assert g.get()["account"]["password"] == "p1"
        print("[ok] GlobalConfig save/mask/reload")

        # 旧版双账号配置自动迁移为统一 account(教务优先)
        g.store.save({"zyfw_account": {"username": "z1", "password": "pz"},
                      "spoc_account": {"username": "s1", "password": "ps"}})
        merged = g.get()["account"]
        assert merged["username"] == "z1" and merged["password"] == "pz", merged
        print("[ok] legacy dual-account migration")

        # 合法 JSON 但根节点不是对象, 应按损坏配置隔离并回退默认值
        g.store.path.write_text("[]", encoding="utf-8")
        assert g.get()["account"]["username"] == ""
        assert g.store.path.with_suffix(".corrupt.json").exists()
        print("[ok] non-object config quarantined")

        g.store.path.write_text('{"account":"invalid"}', encoding="utf-8")
        assert g.get()["account"] == {"username": "", "password": ""}
        g.store.path.write_text(
            '{"account":{"username":"u","password":"dpapi:%%%bad"}}',
            encoding="utf-8")
        assert g.get()["account"]["password"] == ""
        print("[ok] malformed account/token falls back safely")

        shared_path = Path(td) / "shared.json"
        first, second = JsonStore(shared_path), JsonStore(shared_path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda pair: pair[0].update({pair[1]: True}),
                          [(first, "left"), (second, "right")]))
        shared = first.load()
        assert shared.get("left") is True and shared.get("right") is True, shared
        print("[ok] concurrent stores preserve both updates")

    runner = Runner()
    mgr = PluginManager(runner)
    plugins = mgr.list_plugins()
    ids = {p["id"] for p in plugins}
    assert {"course-grab", "spoc-autoplay"} <= ids, ids
    print(f"[ok] PluginManager discovered: {sorted(ids)}")

    # 插件 id 同时用于目录名, 必须拒绝路径穿越。
    try:
        mgr.uninstall("..")
        raise AssertionError("uninstall should reject traversal id")
    except PluginError:
        pass
    print("[ok] plugin id path traversal rejected")

    # zip 根目录直接放 plugin.json 是合法布局; 内置插件 id 不可覆盖。
    with tempfile.TemporaryDirectory(prefix="zyfw-selftest-plugin-") as td:
        plugins_dir = Path(td) / "plugins"
        user_dir = plugins_dir / "installed"
        root_zip = Path(td) / "root-plugin.zip"
        with zipfile.ZipFile(root_zip, "w") as zf:
            zf.writestr("plugin.json", '{"id":"selftest-installed",'
                        '"name":"Test","version":"1.0","entry":"plugin.py"}')
            zf.writestr("plugin.py", "")
        conflict_zip = Path(td) / "builtin-conflict.zip"
        with zipfile.ZipFile(conflict_zip, "w") as zf:
            zf.writestr("plugin.json", '{"id":"course-grab",'
                        '"name":"Conflict","version":"1.0","entry":"plugin.py"}')
            zf.writestr("plugin.py", "")
        with patch.object(plugin_manager_module, "PLUGINS_DIR", plugins_dir), \
                patch.object(plugin_manager_module, "USER_PLUGINS_DIR", user_dir), \
                patch.object(plugin_manager_module, "_USER_PLUGINS_READY", False):
            installed = mgr.install(str(root_zip))
            assert installed["id"] == "selftest-installed", installed
            try:
                mgr.install(str(conflict_zip))
                raise AssertionError("built-in plugin id must be rejected")
            except PluginError:
                pass
    print("[ok] root-level plugin zip installs; built-in id is protected")

    print("ALL BACKEND TESTS PASSED")


if __name__ == "__main__":
    main()
