"""后端核心逻辑自测(不启动服务)。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config_store import (  # noqa: E402
    GlobalConfig, PluginConfigStore, dpapi_protect, dpapi_unprotect, mask_secrets,
)
from app.core.plugin_manager import PluginManager, PluginError  # noqa: E402
from app.core.runner import Runner  # noqa: E402


def main() -> None:
    ok = True

    token = dpapi_protect("hello-密码123")
    assert token.startswith("dpapi:"), token[:10]
    assert dpapi_unprotect(token) == "hello-密码123"
    print("[ok] DPAPI encrypt/decrypt roundtrip")

    g = GlobalConfig()
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

    runner = Runner()
    mgr = PluginManager(runner)
    plugins = mgr.list_plugins()
    ids = {p["id"] for p in plugins}
    assert {"course-grab", "spoc-autoplay"} <= ids, ids
    print(f"[ok] PluginManager discovered: {sorted(ids)}")

    print("ALL BACKEND TESTS PASSED")


if __name__ == "__main__":
    main()
