"""验证:浏览器关闭(WS 断开)→ 宽限期后服务自动退出。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins"))

from _sdk.sdk import make_edge_options  # noqa: E402
from selenium import webdriver  # noqa: E402
from selenium.webdriver.edge.service import Service  # noqa: E402

URL = (ROOT / "data" / "zyfw-gui.url").read_text(encoding="utf-8").strip()


def main() -> None:
    driver = webdriver.Edge(service=Service(), options=make_edge_options(headless=False))
    try:
        driver.get(URL)
        time.sleep(2.5)  # WS 已建立
    finally:
        driver.quit()  # 模拟关闭窗口

    print("浏览器已关闭,等待服务自动退出(宽限 8s + 余量)...")
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            import urllib.request
            urllib.request.urlopen(URL, timeout=1)
            time.sleep(0.5)
        except Exception:
            print("SERVICE EXITED OK")
            return
    raise SystemExit("服务在 20 秒后仍在运行,自动退出失败")


if __name__ == "__main__":
    main()
