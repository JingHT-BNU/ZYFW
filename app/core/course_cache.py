"""course-grab 专业目录与课程列表的本地缓存读取。

缓存文件由插件子进程(selenium 抓取)写入:
- 专业目录(全局共用一份): data/plugins/course-grab/majors.json
- 各专业课程列表: data/plugins/course-grab/courses/<sha1前16位>.json

本模块只读; 写入逻辑在 plugins/course-grab/plugin.py。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
GRAB_DIR = _ROOT / "data" / "plugins" / "course-grab"
CATALOG_FILE = GRAB_DIR / "majors.json"


def course_key(level: str, grade: str, dept: str, major: str) -> str:
    """课程列表缓存文件键: sha1("level|grade|dept|major") 前 16 位。

    注意: 插件侧 plugin.py 有一份相同实现, 修改时两处必须同步。
    """
    raw = "|".join(str(x) for x in (level, grade, dept, major))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def courses_file(level: str, grade: str, dept: str, major: str) -> Path:
    return GRAB_DIR / "courses" / f"{course_key(level, grade, dept, major)}.json"


def load_catalog() -> dict | None:
    """读取专业目录缓存; 文件缺失或损坏返回 None(前端引导用户抓取)。"""
    try:
        data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("levels"), list):
        return None
    return data


def load_courses(level: str, grade: str, dept: str, major: str) -> dict | None:
    """读取某专业的课程列表缓存(含 updated_at 与 courses 字段); 未缓存返回 None。"""
    try:
        data = json.loads(
            courses_file(level, grade, dept, major).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("courses"), list):
        return None
    return data
