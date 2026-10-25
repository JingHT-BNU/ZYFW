"""前端冒烟测试:真实 Edge 加载页面,检查渲染与 JS 错误,截图。"""
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

URL = (ROOT / "data" / "zyfw-gui.url").read_text(encoding="utf-8").strip()


def main() -> None:
    entries = []
    driver = webdriver.Edge(
        service=Service(),
        options=make_edge_options(headless=False),
    )
    try:
        driver.get(URL)
        time.sleep(3)
        entries = driver.get_log("browser")
        severe = [e for e in entries
                  if e["level"] == "SEVERE" and "favicon" not in e["message"]]
        for e in severe:
            print("SEVERE:", e["message"][:300])

        # 仪表盘应渲染出两个插件卡片
        cards = driver.find_elements(By.CSS_SELECTOR, ".plugin-card")
        print(f"plugin cards: {len(cards)}")
        assert len(cards) == 2, f"expect 2 cards, got {len(cards)}"
        assert not severe, f"JS errors: {severe}"

        # 切到全局设置页
        navs = driver.find_elements(By.CSS_SELECTOR, ".nav-item")
        navs[2].click()
        time.sleep(1)
        assert "全局设置" in driver.find_element(By.CSS_SELECTOR, ".page-header h1").text
        print("settings page ok")

        # 打开插件详情(course-grab)
        driver.find_elements(By.CSS_SELECTOR, ".nav-item")[0].click()
        time.sleep(0.5)
        btns = driver.find_elements(By.XPATH, "//button[contains(., '配置 / 日志')]")
        btns[0].click()
        time.sleep(1.5)
        assert driver.find_elements(By.CSS_SELECTOR, ".log-box")
        assert driver.find_elements(By.ID, "cg-load"), "抢课自定义配置界面未加载"
        print("detail page ok")

        driver.save_screenshot(str(ROOT / "data" / "smoke_dashboard.png"))
        print("SMOKE TEST PASSED")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
