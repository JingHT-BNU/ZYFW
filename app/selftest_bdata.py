"""师道云刷课插件离线回归测试。"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

spec = importlib.util.spec_from_file_location(
    "bdata_plugin", ROOT / "plugins" / "bdata-autoplay" / "plugin.py")
bdata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bdata)


def main() -> None:
    url, cid = bdata.parse_course_url(
        "https://bdata.bnu.edu.cn/course?cid=f4cc4730-64f8-11ef-9b8c-c5d211329007")
    assert cid == "f4cc4730-64f8-11ef-9b8c-c5d211329007"
    assert url.startswith("https://bdata.bnu.edu.cn/course?cid=")
    assert bdata.parse_course_url("https://evil.example/course?cid=x") is None
    assert bdata.parse_course_url("http://bdata.bnu.edu.cn/course?cid=x") is None
    print("[ok] course URL validation")

    assert bdata.parse_duration_seconds("观看视频11分26秒") == 686
    assert bdata.parse_duration_seconds("观看视频1小时2分3秒") == 3723
    assert bdata.parse_duration_seconds("632秒") == 632
    assert bdata.parse_duration_seconds("未开始") == 0
    print("[ok] duration label parsing")

    data = {
        "hide": ["hidden-cs", "hidden-part"],
        "cs": [
            {"_id": "v1", "pid": {"_id": "p1", "title": "1.1 视频", "type": 2}},
            {"_id": "v2", "pid": {"_id": "p2", "title": "1.2 视频", "type": 2}},
            {"_id": "test", "pid": {"_id": "p3", "title": "测试", "type": 3}},
            {"_id": "hidden-cs", "pid": {"_id": "p4", "title": "隐藏", "type": 2}},
            {"_id": "hidden-part", "pid": {"_id": "hidden-part",
                                            "title": "隐藏2", "type": 2}},
        ],
    }
    parts = bdata.video_parts(data)
    assert [p["cs_id"] for p in parts] == ["v1", "v2"], parts
    print("[ok] video directory filtering")

    target = bdata.completion_target(686, 684.16)
    assert target == 684.16, target
    assert bdata.is_complete(682, 686, 684.16, 3)
    assert not bdata.is_complete(680, 686, 684.16, 3)
    print("[ok] server completion threshold and tolerance")

    class FakeDriver:
        def set_script_timeout(self, _timeout):
            pass

        def execute_async_script(self, _script, endpoint, _payload):
            if endpoint == bdata.COURSE_DATA_API:
                return {"status": 200, "data": {
                    "e": False, "data": {"title": "课程", "cs": []}}}
            return {"status": 200, "data": {"e": False, "vt": "42.5"}}

    fake = FakeDriver()
    assert bdata.course_data(fake, "cid")["title"] == "课程"
    assert bdata.video_history(fake, "cid", "cs") == 42.5
    print("[ok] course and history response unwrapping")
    print("ALL BDATA PLUGIN TESTS PASSED")


if __name__ == "__main__":
    main()
