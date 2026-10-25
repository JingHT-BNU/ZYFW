"""专业目录 / 课程列表端点自测(不访问教务系统, 只验证缓存读取与任务守卫)。

测试期间备份并恢复真实的 global.json 与 majors.json / courses 缓存。
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.core import course_cache  # noqa: E402
from app.core.config_store import DATA_DIR  # noqa: E402

app = create_app()
client = TestClient(app)

GLOBAL_FILE = DATA_DIR / "global.json"


def _read_bytes(p: Path) -> bytes | None:
    return p.read_bytes() if p.exists() else None


def _write_bytes(p: Path, raw: bytes | None) -> None:
    if raw is None:
        p.unlink(missing_ok=True)
    else:
        p.write_bytes(raw)


def main() -> None:
    # 备份真实文件, 全部用例结束后恢复
    global_bak = _read_bytes(GLOBAL_FILE)
    catalog_bak = _read_bytes(course_cache.CATALOG_FILE)
    course_file = course_cache.courses_file("1", "2025", "001", "080717")
    course_bak = _read_bytes(course_file)
    try:
        # 1. 无缓存时返回 404 并带引导文案
        course_cache.CATALOG_FILE.unlink(missing_ok=True)
        r = client.get("/api/plugins/course-grab/majors")
        assert r.status_code == 404, r.status_code
        assert "抓取专业目录" in r.json()["detail"]
        print("[ok] majors 404 with guide when no cache")

        # 2. 写入样例目录后能读取, 级联结构完整
        sample = {
            "updated_at": "2026-10-13 12:00:00",
            "partial": False,
            "levels": [{
                "v": "1", "t": "本科",
                "grades": [{
                    "v": "2025", "t": "2025",
                    "depts": [{
                        "v": "001", "t": "人工智能学院",
                        "majors": [{"v": "080717", "t": "人工智能"}],
                    }],
                }],
            }],
        }
        course_cache.CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        course_cache.CATALOG_FILE.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
        r = client.get("/api/plugins/course-grab/majors")
        assert r.status_code == 200, r.text
        assert r.json()["levels"][0]["grades"][0]["depts"][0]["majors"][0]["t"] == "人工智能"
        print("[ok] majors catalog read from cache")

        # 3. 课程列表: 未缓存 404, 参数不完整 400
        r = client.get("/api/plugins/course-grab/major-courses?level=1&grade=2025&dept=001&major=080717")
        assert r.status_code == 404, r.status_code
        r = client.get("/api/plugins/course-grab/major-courses?level=1")
        assert r.status_code == 400, r.status_code
        print("[ok] major-courses 404/400 guards")

        # 4. 未配置账号时抓取任务被 400 拒绝(临时清空 global.json 的账号)
        blank = {"account": {"username": "", "password": ""}}
        GLOBAL_FILE.write_text(json.dumps(blank, ensure_ascii=False), encoding="utf-8")
        r = client.post("/api/plugins/course-grab/majors/refresh")
        assert r.status_code == 400, (r.status_code, r.text)
        assert "账号" in r.json()["detail"]
        print("[ok] refresh rejected without account")

        # 4b. 账号合并回归: 插件配置的 username/password 键总是存在(留空=空串占位),
        # 合并逻辑必须用空串回退到全局账号, 而不是被 setdefault 跳过。
        # (此前 setdefault 不覆盖空串, 导致已填全局账号仍提示"请先填写账号")
        GLOBAL_FILE.write_text(json.dumps(
            {"account": {"username": "stu001", "password": "globalsecret"}},
            ensure_ascii=False), encoding="utf-8")
        from app.api import _effective_plugin_config
        merged = _effective_plugin_config(
            {"dir": "x"}, {"username": "", "password": "",
                           "account": {"username": "", "password": ""}})
        assert merged["username"] == "stu001", merged
        assert merged["password"] == "globalsecret", merged
        assert merged["account"]["username"] == "stu001", merged
        # 插件级账号优先于全局
        merged2 = _effective_plugin_config(
            {"dir": "x"}, {"username": "plug", "password": "plugpw",
                           "account": {"username": "plug", "password": "plugpw"}})
        assert merged2["username"] == "plug" and merged2["password"] == "plugpw", merged2
        print("[ok] empty plugin account falls back to global account")

        # 5. 课程缓存读写(与插件侧同键路径)
        data = {
            "updated_at": "2026-10-13 12:00:00",
            "specialty": {"level": {"v": "1", "t": "本科"},
                          "grade": {"v": "2025", "t": "2025"},
                          "dept": {"v": "001", "t": "人工智能学院"},
                          "major": {"v": "080717", "t": "人工智能"}},
            "total_pages": 2,
            "courses": [
                {"cells": ["1", "GEN06122 生活方式与社会", "2", "32", "1-16周", "线上", "张三", "周三 5-6", "线上"],
                 "extra": [["", "", "", "1-8周 单周", "机房A"]],
                 "code": "GEN06122"},
            ],
        }
        course_file.parent.mkdir(parents=True, exist_ok=True)
        course_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        r = client.get("/api/plugins/course-grab/major-courses?level=1&grade=2025&dept=001&major=080717")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["courses"][0]["code"] == "GEN06122"
        assert body["courses"][0]["extra"][0][3] == "1-8周 单周"
        print("[ok] major-courses cache roundtrip")

        # 6. 缓存键稳定: sha1("level|grade|dept|major")[:16]
        import hashlib
        raw = "|".join(("1", "2025", "001", "080717"))
        expect = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        assert course_cache.course_key("1", "2025", "001", "080717") == expect
        assert course_file.name == f"{expect}.json"
        print("[ok] course cache key stable (sha1[:16])")

        # 7. 自定义配置界面端点: 下发 HTML 且剥离 <style>; 无 config_ui 的插件 404
        r = client.get("/api/plugins/course-grab/ui")
        assert r.status_code == 200, r.text
        html = r.json()["html"]
        assert "cg-level" in html and "<style" not in html.lower()
        assert 'style="' not in html.lower()
        assert "ZyfwConfigUI" in html
        print("[ok] plugin ui endpoint serves stripped html")
        r = client.get("/api/plugins/spoc-autoplay/ui")
        assert r.status_code == 404, r.status_code
        print("[ok] ui endpoint 404 for schema-only plugin")

        print("ALL CATALOG TESTS PASSED")
    finally:
        _write_bytes(GLOBAL_FILE, global_bak)
        _write_bytes(course_cache.CATALOG_FILE, catalog_bak)
        _write_bytes(course_file, course_bak)


if __name__ == "__main__":
    main()
