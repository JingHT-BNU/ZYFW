"""最终 UI 走查:SPOC 2.0 插件详情页渲染确认 + 截图。"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins"))

from _sdk.sdk import make_edge_options  # noqa: E402
from selenium import webdriver  # noqa: E402
from selenium.webdriver.edge.service import Service  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402

OUT = ROOT / "data" / "ui_shots"
OUT.mkdir(parents=True, exist_ok=True)
URL = (ROOT / "data" / "zyfw-gui.url").read_text(encoding="utf-8").strip()
PLUGIN_VERSION = json.loads(
    (ROOT / "plugins" / "spoc-autoplay" / "plugin.json").read_text(encoding="utf-8")
)["version"]

driver = webdriver.Edge(service=Service(), options=make_edge_options(headless=False))
driver.set_window_size(1400, 900)
try:
    driver.get(URL)
    time.sleep(3)
    driver.find_elements(By.CSS_SELECTOR, ".nav-item")[0].click()
    time.sleep(0.5)
    clicked = False
    for b in driver.find_elements(By.XPATH, "//button[contains(., '配置 / 日志')]"):
        card_text = b.find_element(By.XPATH, "ancestor::div[contains(@class,'plugin-card')]").text
        if "SPOC" in card_text:
            b.click()
            clicked = True
            break
    assert clicked, "未找到 SPOC 卡片按钮"
    time.sleep(1.5)
    ver = driver.find_element(By.CSS_SELECTOR, ".page-header .el-tag").text
    labels = [e.text for e in driver.find_elements(By.CSS_SELECTOR, ".el-form-item__label")]
    print("version tag:", ver)
    print("labels:", labels)
    assert ver.lstrip("v") == PLUGIN_VERSION, (ver, PLUGIN_VERSION)
    assert any("学习内容页" in l for l in labels)
    assert any("只刷未完成" in l for l in labels)
    driver.save_screenshot(str(OUT / "plugin20_detail.png"))
    print("UI CHECK PASSED, SHOT SAVED")
finally:
    driver.quit()
