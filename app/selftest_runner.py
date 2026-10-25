"""验证 Runner:启动假插件 → 采集日志与 progress 事件 → 停止。(自包含,自动生成假插件)"""
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.runner import Runner  # noqa: E402

DUMMY_DIR = ROOT / "data" / ".selftest" / "dummy-plugin"

DUMMY_JSON = {
    "id": "selftest-dummy",
    "name": "自测假插件",
    "version": "0.0.1",
    "entry": "plugin.py",
    "config_schema": [],
}
DUMMY_PY = '''"""假插件:验证 runner 启停、日志采集、进度事件解析。"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _sdk.sdk import PluginContext


def main():
    ctx = PluginContext()
    n = int(ctx.cfg("n", 3))
    for i in range(n):
        ctx.log(f"tick {i + 1}/{n}")
        ctx.progress(done=i + 1, total=n)
        time.sleep(0.2)
    time.sleep(0.5)  # 留出观察窗口: 最后一次 progress 先于退出被 snapshot 捕获
    print("done")


if __name__ == "__main__":
    main()
'''


def main() -> None:
    DUMMY_DIR.mkdir(parents=True, exist_ok=True)
    (DUMMY_DIR / "plugin.json").write_text(
        json.dumps(DUMMY_JSON, ensure_ascii=False, indent=2), encoding="utf-8")
    (DUMMY_DIR / "plugin.py").write_text(DUMMY_PY, encoding="utf-8")

    runner = Runner()
    bus = runner.bus
    seen: list[dict] = []
    bus.subscribe(lambda e: seen.append(e))

    try:
        runner.start(
            "selftest-dummy",
            DUMMY_DIR / "plugin.py",
            {"n": 3, "general": {}},
            ROOT / "data" / ".selftest" / "dummy-data",
            env_extra={"PYTHONPATH": str(ROOT / "plugins")},
        )

        # 阶段1: 等最近一次 progress 事件经 snapshot 暴露(done==3)
        deadline = time.time() + 15
        last_progress = None
        while time.time() < deadline:
            st = runner.snapshot().get("selftest-dummy", {})
            if st.get("progress"):
                last_progress = dict(st["progress"])
            if last_progress and last_progress.get("done") == 3:
                break
            time.sleep(0.1)

        assert last_progress, "snapshot 未暴露 progress"
        assert last_progress.get("done") == 3 and last_progress.get("total") == 3
        assert "__event__" not in last_progress, last_progress
        assert "plugin" not in last_progress, last_progress
        assert isinstance(last_progress.get("ts"), float), last_progress

        # 阶段2: 等进程退出并收到终态事件(终态事件发布于清除 progress 之后)
        deadline = time.time() + 15
        while time.time() < deadline:
            evs = [e for e in seen if e["type"] == "plugin_status"]
            if evs and evs[-1]["status"] in ("exited", "stopped", "crashed"):
                break
            time.sleep(0.1)

        types = [e["type"] for e in seen]
        logs = [e for e in seen if e["type"] == "log"]
        progress = [e for e in seen if e["type"] == "progress"]
        final_status = [e for e in seen if e["type"] == "plugin_status"][-1]

        assert logs, f"未采集到日志: {types}"
        assert any("tick 1/3" in e["line"] for e in logs), [e.get("line") for e in logs]
        assert len(progress) >= 3, f"progress 事件不足: {len(progress)}"
        assert progress[-1]["done"] == 3 and progress[-1]["total"] == 3
        assert final_status["status"] == "exited", final_status
        assert final_status["exit_code"] == 0
        # 进程结束后 progress 清除, /api/plugins 不残留陈旧进度
        snap = runner.snapshot()
        assert snap["selftest-dummy"]["progress"] is None, snap["selftest-dummy"]

        print(f"events: {len(seen)}, logs: {len(logs)}, progress: {len(progress)}")
        print("RUNNER TEST PASSED")
    finally:
        shutil.rmtree(ROOT / "data" / ".selftest", ignore_errors=True)


if __name__ == "__main__":
    main()
