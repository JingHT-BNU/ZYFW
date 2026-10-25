"""UI 走查 2.1:真实运行插件,SPOC 详情页每线程面板渲染确认 + 截图。

前置:GUI 服务已启动(URL 在 data/zyfw-gui.url)。脚本自行启停插件并还原配置。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins"))

from _sdk.sdk import make_edge_options  # noqa: E402
from selenium import webdriver  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.edge.service import Service  # noqa: E402

BASE = (ROOT / "data" / "zyfw-gui.url").read_text(encoding="utf-8").strip()
OUT = ROOT / "data" / "ui_shots"
OUT.mkdir(parents=True, exist_ok=True)
PLUGIN_VERSION = json.loads(
    (ROOT / "plugins" / "spoc-autoplay" / "plugin.json").read_text(encoding="utf-8")
)["version"]


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        BASE + "api" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def main() -> None:
    cfg = api("/plugins/spoc-autoplay/config")["config"]
    orig = json.loads(json.dumps(cfg))
    cfg.update(only_unfinished=False, workers=2, speedup=16)
    api("/plugins/spoc-autoplay/config", "PUT", {"config": cfg})
    api("/plugins/spoc-autoplay/start", "POST")
    print("[setup] plugin started (2 workers, 16x, force-replay)")

    driver = webdriver.Edge(service=Service(), options=make_edge_options(headless=False))
    try:
        driver.set_window_size(1440, 940)
        driver.get(BASE)
        time.sleep(3)
        driver.find_elements(By.CSS_SELECTOR, ".nav-item")[0].click()
        time.sleep(0.5)
        clicked = False
        for b in driver.find_elements(By.XPATH, "//button[contains(., '配置 / 日志')]"):
            card_text = b.find_element(
                By.XPATH, "ancestor::div[contains(@class,'plugin-card')]").text
            if "SPOC" in card_text:
                b.click()
                clicked = True
                break
        assert clicked, "未找到 SPOC 卡片按钮"

        ver = driver.find_element(By.CSS_SELECTOR, ".page-header .el-tag").text
        assert ver.lstrip("v") == PLUGIN_VERSION, (ver, PLUGIN_VERSION)
        print("[ok] version tag:", ver)

        # 等待线程面板出现(插件首个视频开播后即有 progress 事件)
        panels = []
        deadline = time.time() + 90
        while time.time() < deadline:
            panels = driver.find_elements(By.CSS_SELECTOR, ".worker-panel")
            if panels:
                break
            time.sleep(2)
        assert panels, "90s 内未渲染出线程面板"
        time.sleep(4)  # 等进度条/元数据填充

        n_panels = len(panels)
        metas = driver.find_elements(By.CSS_SELECTOR, ".worker-panel .wp-meta")
        bars = driver.find_elements(
            By.CSS_SELECTOR, ".worker-panel .el-progress-bar")
        titles = [e.text for e in driver.find_elements(
            By.CSS_SELECTOR, ".worker-panel .wp-title")]
        print(f"[ok] worker panels: {n_panels}, metas: {len(metas)}, bars: {len(bars)}")
        print("[ok] titles:", titles)
        assert n_panels >= 1
        assert metas, "面板缺 wp-meta(进度/时长/ETA 文本)"
        if any(t.strip() not in ("", "—") for t in titles):
            print("[ok] thread titles shown")

        # 面板滚动进视图并截图
        driver.execute_script(
            "document.querySelector('.worker-panels').scrollIntoView({block:'center'});")
        time.sleep(1)
        driver.save_screenshot(str(OUT / "ui21_worker_panels.png"))
        print("UI 2.1 CHECK PASSED, SHOT SAVED")
    finally:
        driver.quit()
        try:
            api("/plugins/spoc-autoplay/stop", "POST")
            time.sleep(3)
        except Exception:
            pass
        api("/plugins/spoc-autoplay/config", "PUT", {"config": orig})
        print("[teardown] plugin stopped, config restored")


if __name__ == "__main__":
    main()
