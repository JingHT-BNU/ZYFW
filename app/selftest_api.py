"""API 层集成测试:配置读写、密码掩码保持、插件启停、WebSocket。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.core.config_store import GlobalConfig  # noqa: E402

# 先清理自测残留的全局配置
g = GlobalConfig()
(g.store.path).unlink(missing_ok=True)

app = create_app()
client = TestClient(app)


def main() -> None:
    # 1. 全局配置 PUT -> GET,密码回读打码
    r = client.put("/api/global", json={"config": {
        "account": {"username": "stu001", "password": "secret123"}}})
    assert r.status_code == 200, r.text
    r = client.get("/api/global")
    cfg = r.json()["config"]
    assert cfg["account"]["username"] == "stu001"
    assert cfg["account"]["password"] == "******"
    print("[ok] global PUT/GET with masking")

    # 密码传 ****** 不应覆盖真实值
    r = client.put("/api/global", json={"config": {
        "account": {"username": "stu001", "password": "******"}}})
    assert r.status_code == 200
    real = g.get()["account"]["password"]
    assert real == "secret123", real
    print("[ok] masked password preserved on update")

    # 2. 插件配置 PUT -> GET
    r = client.put("/api/plugins/course-grab/config", json={"config": {
        "mode": "auto",
        "courses": {"GEN06122": ["09", "10"]},
        "poll_interval": 3,
        "headless": True,
        "username": "",
        "password": "plugsecret",
    }})
    assert r.status_code == 200, r.text
    r = client.get("/api/plugins/course-grab/config")
    body = r.json()
    assert body["schema"][0]["key"] == "mode"
    assert body["config"]["password"] == "******"
    assert body["config"]["courses"] == {"GEN06122": ["09", "10"]}
    print("[ok] plugin config PUT/GET with schema")

    # 3. 启动插件(未配课程 → 插件应立刻退出而非报错)
    r = client.put("/api/plugins/course-grab/config", json={"config": {
        "mode": "auto", "courses": {}, "poll_interval": 2, "headless": True,
        "username": "", "password": ""}})
    assert r.status_code == 200
    r = client.post("/api/plugins/course-grab/start")
    assert r.status_code == 200, r.text

    # 重复启动应 409
    r2 = client.post("/api/plugins/course-grab/start")
    assert r2.status_code == 409, r2.status_code

    deadline = time.time() + 15
    status = "running"
    while time.time() < deadline:
        r = client.get("/api/plugins")
        p = [x for x in r.json()["plugins"] if x["id"] == "course-grab"][0]
        if not p["running"]:
            status = p["status"]
            break
        time.sleep(0.3)
    assert status in ("exited", "crashed"), status

    r = client.get("/api/plugins/course-grab/logs?limit=100")
    lines = [e.get("line", "") for e in r.json()["events"] if e["type"] == "log"]
    assert any("未配置任何课程" in ln for ln in lines), lines
    print("[ok] plugin start/stop lifecycle via API, logs captured")

    # 4. WebSocket: hello 历史回放
    with client.websocket_connect("/api/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert isinstance(hello["history"], list)
    print(f"[ok] websocket hello, {len(hello['history'])} history events")

    print("ALL API TESTS PASSED")


if __name__ == "__main__":
    main()
