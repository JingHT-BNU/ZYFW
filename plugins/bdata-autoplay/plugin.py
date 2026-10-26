"""师道云课程视频自动播放插件。

站点关键行为(2026-10 实测):
- 未登录时课程页提供“北师大统一身份认证登录”入口, 点击后进入 CAS;
- 任务进度窗口按播放图标筛选必修视频, 选修视频无需处理;
- 播放页时长标签会随服务器记录增长;
- 完成判定只比较该文字与本地视频最大时长, 不使用本地 currentTime。
"""
from __future__ import annotations

import json
import os
import queue
import random
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sdk.sdk import (  # noqa: E402
    PluginContext,
    make_driver,
    find,
    find_strict,
)
from selenium.common.exceptions import WebDriverException  # noqa: E402
from selenium.webdriver.common.action_chains import ActionChains  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402

from llm import (  # noqa: E402
    LLMError,
    generate_interaction,
    generate_quiz_answers,
)

LOGIN_HOME = "https://bdata.bnu.edu.cn/"
COURSE_DATA_API = "/wechatapi/api/learn/getCourseData"
COOKIE_LIFE_SECONDS = 7 * 86400
TASK_CACHE_VERSION = 2

TASK_BUTTON_XPATH = (
    '//*[@id="app"]/section/main/div/div[2]/div[2]/div/div/div[3]'
    '/span/button[1]'
)
TASK_VIDEO_ICON_PREFIX = "M512 64a448"
TASK_TEST_ICON_PREFIX = "m199.04 672.64 1"
DURATION_XPATH = (
    '//*[@id="app"]/section/main/div/div[2]/div[2]/div/div[1]'
    '/div[3]/span/div/span[4]/span'
)
LOGIN_BUTTON_XPATH = (
    '//button[contains(normalize-space(.), "北师大统一身份认证登录")]'
)
USERNAME_XPATH = (
    '//input[@id="username" or @name="username" '
    'or contains(@placeholder, "学工号") or contains(@placeholder, "学号") '
    'or contains(@placeholder, "邮箱")]'
)
PASSWORD_XPATH = '//input[@type="password" or @id="password" or @name="password"]'
SUBMIT_XPATH = (
    '//button[contains(., "登录")] | //a[contains(., "登录")] '
    '| //input[@type="submit"]'
)
QUIZ_SUBMIT_XPATH = (
    '//*[@id="app"]/section/main/div/div[2]/div[3]/div[2]'
    '/div/div/div[1]/div/div/button'
)
SURVEY_QUESTIONS = (
    "本章节的学习内容让我觉得很无聊。",
    "因学习内容枯燥，我在学习过程中非常容易分心。",
    "学习时，我会感到紧张和焦虑。",
    "我认为本章所学内容能应用到实际生活或未来的工作中。",
    "我对必须完成的学习任务感到厌烦。",
)
SURVEY_VALENCES = (-1, -1, -1, 1, -1)
SURVEY_OPTIONS = (
    ("0", "非常不同意"),
    ("1", "不同意"),
    ("2", "一般"),
    ("3", "同意"),
    ("4", "非常同意"),
)

DURATION_RE = re.compile(
    r"(?:观看视频|视频时长|学习时长)?\s*"
    r"(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分)?\s*(?:(\d+)\s*秒)?"
)

_FETCH_JSON_JS = """
const done = arguments[arguments.length - 1];
fetch(arguments[0], {
    method: 'POST',
    credentials: 'include',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(arguments[1])
}).then(async resp => {
    const text = await resp.text();
    let data;
    try { data = JSON.parse(text); }
    catch (e) { data = {e: true, m: text || ('HTTP ' + resp.status)}; }
    done({status: resp.status, data});
}).catch(e => done({status: 0, data: {e: true, m: String(e)}}));
"""


def normalize_title(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\xa0", " ")).strip()


def parse_course_url(url: object) -> tuple[str, str] | None:
    """Validate a bdata course URL and return (normalized_url, cid)."""
    raw = str(url or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if (parsed.scheme != "https" or parsed.hostname != "bdata.bnu.edu.cn"
            or parsed.path.rstrip("/") != "/course"):
        return None
    cids = parse_qs(parsed.query).get("cid") or []
    cid = cids[0].strip() if cids else ""
    if not cid:
        return None
    return f"https://bdata.bnu.edu.cn/course?cid={cid}", cid


def parse_duration_seconds(text: object) -> int:
    """Parse '观看视频11分26秒' / '1小时2分3秒' / '632秒'."""
    raw = normalize_title(text)
    if not raw:
        return 0
    m = DURATION_RE.search(raw)
    if not m:
        return 0
    hours, minutes, seconds = (int(v or 0) for v in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def bounded_float(value: object, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


def positive_int(value: object, default: int) -> int:
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return max(1, default)


def completion_gap(media_duration: float, tolerance: float) -> float:
    """Allow explicit tolerance plus 1% for whole-second server rounding."""
    return max(max(0.0, tolerance), max(0.0, media_duration) * 0.01)


def is_complete(watched: float, media_duration: float, tolerance: float) -> bool:
    return (media_duration > 0
            and watched + completion_gap(media_duration, tolerance) >= media_duration)


def survey_choice(value: object, default: int) -> str:
    raw = str(value if value is not None else default).strip()
    for option_value, label in SURVEY_OPTIONS:
        if raw == label:
            return option_value
    try:
        number = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return str(default)
    return str(min(4, max(0, number)))


def survey_normal_value(z_score: float | None = None) -> str:
    """Map a standard-normal draw onto the five-point Likert scale.

    The mean is "一般". A value at 2 sigma is "同意/不同意", and a value
    at 3 sigma is "非常同意/非常不同意".
    """
    z = random.gauss(0.0, 1.0) if z_score is None else float(z_score)
    if z >= 3:
        return "4"
    if z >= 2:
        return "3"
    if z <= -3:
        return "0"
    if z <= -2:
        return "1"
    return "2"


def cookie_file(ctx: PluginContext) -> Path:
    return Path(ctx.datadir) / "cookies.json"


def save_cookies(ctx: PluginContext, cookies: list[dict]) -> None:
    path = cookie_file(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=".bdata-cookies-", suffix=".tmp", delete=False,
        ) as f:
            tmp_path = f.name
            json.dump({"saved_at": time.time(), "cookies": cookies},
                      f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def load_cookies(ctx: PluginContext) -> list[dict]:
    path = cookie_file(ctx)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data.get("saved_at", 0)) > COOKIE_LIFE_SECONDS:
            return []
        cookies = data.get("cookies")
        return cookies if isinstance(cookies, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}-", suffix=".tmp",
                delete=False) as handle:
            tmp_path = handle.name
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _read_json_object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


class ReviewBroker:
    """Pause automation until the local configuration UI approves a draft."""

    def __init__(self, ctx: PluginContext):
        self.ctx = ctx
        self.request_path = Path(ctx.datadir) / "review_request.json"
        self.response_path = Path(ctx.datadir) / "review_response.json"

    def clear(self) -> None:
        for path in (self.request_path, self.response_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def ask(self, kind: str, title: str, fields: list[dict],
            context: list[str] | None = None,
            timeout: float = 24 * 3600) -> dict | None:
        self.clear()
        request = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "title": title,
            "fields": fields,
            "context": context or [],
            "created_at": time.time(),
        }
        _write_json_atomic(self.request_path, request)
        self.ctx.status("等待用户核验模型生成内容…", review_id=request["id"])
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                response = _read_json_object(self.response_path)
                if response and response.get("request_id") == request["id"]:
                    try:
                        self.response_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return response
                time.sleep(0.5)
        finally:
            try:
                self.request_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                self.response_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise TimeoutError("等待用户核验超时")


def inject_cookies(driver, cookies: list[dict]) -> None:
    if not cookies:
        return
    driver.get(LOGIN_HOME)
    driver.delete_all_cookies()
    for cookie in cookies:
        try:
            driver.add_cookie(cookie)
        except WebDriverException:
            pass


def api_json(driver, endpoint: str, payload: dict) -> dict:
    driver.set_script_timeout(30)
    result = driver.execute_async_script(_FETCH_JSON_JS, endpoint, payload)
    if not isinstance(result, dict):
        raise RuntimeError(f"{endpoint} 返回格式异常")
    body = result.get("data")
    if not isinstance(body, dict):
        raise RuntimeError(f"{endpoint} 返回非 JSON 数据")
    if result.get("status") != 200 or body.get("e"):
        raise RuntimeError(
            f"{endpoint} 请求失败({result.get('status')}): "
            f"{body.get('m') or '未登录或会话已失效'}")
    return body


def course_data(driver, cid: str) -> dict:
    body = api_json(driver, COURSE_DATA_API, {"cid": cid})
    data = body.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("课程数据响应缺少 data 对象")
    return data


def _task_video_entries(driver) -> list[dict]:
    js = """
    const pane = document.getElementById('pane-first');
    if (!pane) return [];
    const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
    const out = [];
    for (const span of pane.querySelectorAll('span.el-text')) {
        const icon = span.querySelector('i > svg > path');
        if (!icon) continue;
        const path = icon.getAttribute('d') || '';
        if (!path.startsWith(arguments[0])) continue;
        const clone = span.cloneNode(true);
        clone.querySelectorAll('.el-tag').forEach(el => el.remove());
        const title = norm(clone.textContent);
        const watched = Array.from(span.querySelectorAll('.el-tag__content'))
            .map(el => norm(el.textContent))
            .find(text => text.startsWith('观看视频')) || '';
        if (title) out.push({title, watched});
    }
    return out;
    """
    rows = driver.execute_script(js, TASK_VIDEO_ICON_PREFIX)
    return rows if isinstance(rows, list) else []


def _scroll_task_pane(driver) -> bool:
    js = """
    const pane = document.getElementById('pane-first');
    const dialog = pane && pane.closest('.el-dialog');
    if (!dialog) return false;
    const candidates = Array.from(dialog.querySelectorAll('*')).filter(el =>
        el.scrollHeight > el.clientHeight + 10 &&
        ['auto', 'scroll'].includes(getComputedStyle(el).overflowY));
    if (!candidates.length) return false;
    const el = candidates.sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 2) return false;
    el.scrollTop = Math.min(el.scrollHeight, el.scrollTop + el.clientHeight * 0.8);
    return true;
    """
    return bool(driver.execute_script(js))


def _task_pane_ready(driver, require_cards: bool = True) -> bool:
    return bool(driver.execute_script(
        """
        const pane = document.getElementById('pane-first');
        if (!pane) return false;
        const visible = !!(pane.offsetWidth || pane.offsetHeight || pane.getClientRects().length);
        if (!visible) return false;
        if (!arguments[0]) return true;
        return !!pane.querySelector('.el-card span.el-text i > svg > path');
        """,
        require_cards,
    ))


def open_task_dialog(driver) -> None:
    last_error: Exception | None = None
    for _ in range(4):
        time.sleep(0.8)
        button = find(driver, TASK_BUTTON_XPATH, timeout=10)
        if button is None:
            button = find(
                driver,
                '//button[contains(normalize-space(.), "查看任务进度")]',
                timeout=5,
            )
        if button is None:
            raise RuntimeError("未找到“查看任务进度”按钮")
        try:
            driver.execute_script("arguments[0].click();", button)
        except WebDriverException as exc:
            last_error = exc
            time.sleep(0.3)
            continue
        deadline = time.time() + 8
        while time.time() < deadline:
            if _task_pane_ready(driver):
                time.sleep(0.5)
                if _task_pane_ready(driver):
                    return
            time.sleep(0.2)
        if _task_pane_ready(driver, require_cards=False):
            try:
                close_task_dialog(driver, reload_on_failure=True)
            except RuntimeError:
                pass
    raise RuntimeError(f"任务进度窗口未能打开: {last_error}")


def _task_groups_snapshot(driver) -> list[dict]:
    js = """
    const pane = document.getElementById('pane-first');
    if (!pane) return [];
    const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
    const cards = Array.from(pane.children)
        .filter(el => el.classList && el.classList.contains('el-card'));
    return cards.map((card, groupIndex) => {
        const body = Array.from(card.children)
            .find(el => el.classList && el.classList.contains('el-card__body'));
        if (!body) return {groupIndex, header: '', metrics: [], items: []};
        const spans = Array.from(body.querySelectorAll('span.el-text'));
        const headers = spans.filter(span => !span.querySelector('i > svg > path'));
        const items = spans.filter(span => span.querySelector('i > svg > path'))
            .map(span => {
                const clone = span.cloneNode(true);
                clone.querySelectorAll('.el-tag').forEach(el => el.remove());
                const icon = span.querySelector('i > svg > path');
                const tags = Array.from(span.querySelectorAll('.el-tag__content'))
                    .map(el => norm(el.textContent));
                return {
                    title: norm(clone.textContent),
                    icon: icon ? (icon.getAttribute('d') || '').slice(0, 16) : '',
                    tags,
                };
            }).filter(item => item.title);
        return {
            groupIndex,
            header: headers.map(el => norm(el.innerText)).join(' '),
            metrics: headers.flatMap(el =>
                Array.from(el.querySelectorAll('.el-tag__content'))
                    .map(tag => norm(tag.textContent))),
            items,
        };
    }).filter(group => group.items.length || group.header);
    """
    groups = driver.execute_script(js)
    return groups if isinstance(groups, list) else []


def collect_task_groups(driver) -> list[dict]:
    open_task_dialog(driver)
    merged: dict[str, dict] = {}
    order: list[str] = []
    try:
        for _ in range(40):
            groups = _task_groups_snapshot(driver)
            if not groups:
                time.sleep(0.3)
                continue
            for group in groups:
                header = normalize_title(group.get("header"))
                key = header or f"group-{group.get('groupIndex')}"
                if key not in merged:
                    merged[key] = {
                        "header": header,
                        "metrics": [],
                        "items": [],
                    }
                    order.append(key)
                merged[key]["metrics"] = list(dict.fromkeys(
                    merged[key]["metrics"] + group.get("metrics", [])))
                seen = {item["title"] for item in merged[key]["items"]}
                for item in group.get("items", []):
                    if item.get("title") not in seen:
                        merged[key]["items"].append(item)
                        seen.add(item.get("title"))
            if not _scroll_task_pane(driver):
                break
            time.sleep(0.15)
    finally:
        close_task_dialog(driver, reload_on_failure=True)
    return [merged[key] for key in order]


def task_group_has(group: dict, name: str) -> bool:
    prefix = f"{name}0/"
    return any(str(metric).startswith(prefix) for metric in group.get("metrics", []))


def click_task_entry(driver, title: str) -> None:
    js = """
    const pane = document.getElementById('pane-first');
    if (!pane) return false;
    if (!(pane.offsetWidth || pane.offsetHeight || pane.getClientRects().length)) {
        return false;
    }
    const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
    const target = norm(arguments[0]);
    for (const span of pane.querySelectorAll('span.el-text')) {
        const clone = span.cloneNode(true);
        clone.querySelectorAll('.el-tag').forEach(el => el.remove());
        if (norm(clone.textContent) !== target) continue;
        span.scrollIntoView({block: 'center'});
        span.click();
        return true;
    }
    return false;
    """
    if not driver.execute_script(js, title):
        raise RuntimeError(f"任务进度中未找到: {title}")
    deadline = time.time() + 15
    while time.time() < deadline:
        if not _task_pane_ready(driver, require_cards=False):
            wait_active_menu_title(driver, title, timeout=5)
            time.sleep(1.0)
            return
        time.sleep(0.2)
    close_task_dialog(driver)
    wait_active_menu_title(driver, title, timeout=10)
    time.sleep(1.0)


def collect_task_videos(driver) -> list[dict]:
    """Open the task dialog and collect required videos by their play icon."""
    videos: dict[str, dict] = {}
    for group in collect_task_groups(driver):
        for item in group.get("items", []):
            if item.get("icon", "").startswith(TASK_VIDEO_ICON_PREFIX):
                title = normalize_title(item.get("title"))
                videos[title] = {"title": title, "watched": ""}
    return list(videos.values())


def task_cache_path(ctx: PluginContext) -> Path:
    return Path(ctx.datadir) / "course_tasks.json"


def load_task_cache(ctx: PluginContext) -> dict:
    data = _read_json_object(task_cache_path(ctx))
    if not data or data.get("version") != TASK_CACHE_VERSION:
        return {"version": TASK_CACHE_VERSION, "updated_at": "", "courses": {}}
    if not isinstance(data.get("courses"), dict):
        data["courses"] = {}
    return data


def save_task_cache(ctx: PluginContext, data: dict) -> None:
    _write_json_atomic(task_cache_path(ctx), data)


def collect_course_menu_videos(driver) -> list[dict]:
    deadline = time.time() + 8
    videos: list[dict] = []
    while time.time() < deadline:
        videos = driver.execute_script(
            """
            const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
            return Array.from(document.querySelectorAll('li.el-menu-item'))
              .map(el => {
                const path = el.querySelector('svg path');
                return {
                  title: norm(el.innerText),
                  icon: path ? (path.getAttribute('d') || '') : '',
                };
              })
              .filter(item => item.title &&
                  item.icon.startsWith(arguments[0]));
            """,
            TASK_VIDEO_ICON_PREFIX,
        )
        videos = videos if isinstance(videos, list) else []
        if videos:
            break
        time.sleep(0.3)
    result: dict[str, dict] = {}
    for video in videos:
        title = normalize_title(video.get("title"))
        if title:
            result[title] = {"title": title, "outside_tasks": True}
    return list(result.values())


def _task_metric_counts(metrics: list[str]) -> dict[str, dict]:
    counts: dict[str, dict] = {}
    for raw in metrics:
        match = re.match(r"^(.+?)(\d+)/(\d+)$", normalize_title(raw))
        if not match:
            continue
        name, done, total = match.groups()
        counts[name] = {"done": int(done), "total": int(total)}
    return counts


def _task_completion(metrics: list[str], counts: dict[str, dict]) -> dict:
    video_percent = None
    for metric in metrics:
        match = re.search(r"观看视频(\d+)%", metric)
        if match:
            video_percent = int(match.group(1))
            break
    requirements_done = all(
        item["done"] >= item["total"] for item in counts.values())
    complete = requirements_done and (
        video_percent is None or video_percent >= 100)
    return {
        "video_percent": video_percent,
        "questionnaires": counts.get("提交问卷"),
        "comments": counts.get("评论"),
        "replies": counts.get("回复"),
        "complete": complete,
    }


def _task_cache_group(cid: str, group: dict, index: int) -> dict:
    header = normalize_title(group.get("header"))
    metrics = [normalize_title(m) for m in group.get("metrics", [])]
    first_metric = re.search(
        r"(?:提交问卷|评论|回复|观看视频)\d+/\d+", header)
    description = header[:first_metric.start()].strip(
    ) if first_metric else header
    number_match = re.search(r"任务\s*(\d+)", description)
    number = int(number_match.group(1)) if number_match else index + 1
    videos, tests, summaries, items_out = [], [], [], []
    for item in group.get("items", []):
        entry = {
            "title": normalize_title(item.get("title")),
            "tags": [normalize_title(tag) for tag in item.get("tags", [])],
            "icon": str(item.get("icon") or ""),
        }
        icon = str(item.get("icon") or "")
        items_out.append(entry)
        if icon.startswith(TASK_VIDEO_ICON_PREFIX):
            videos.append(entry)
        elif icon.startswith(TASK_TEST_ICON_PREFIX):
            tests.append(entry)
        elif icon.startswith("M832 384H576V128"):
            summaries.append(entry)
    requirements = _task_metric_counts(metrics)
    return {
        "id": f"{cid}:task:{number}",
        "number": number,
        "name": f"任务{number}",
        "description": description,
        "metrics": metrics,
        "requirements": requirements,
        "completion": _task_completion(metrics, requirements),
        "items": items_out,
        "videos": videos,
        "tests": tests,
        "summaries": summaries,
    }


def cache_course_tasks(ctx: PluginContext, driver, course_url: str,
                       cid: str) -> dict:
    driver.get(course_url)
    time.sleep(1.0)
    data = course_data(driver, cid)
    title = normalize_title(data.get("title")) or cid
    groups = collect_task_groups(driver)
    tasks = [
        _task_cache_group(cid, group, index)
        for index, group in enumerate(groups)
    ]
    task_video_titles = {
        video["title"] for task in tasks for video in task["videos"]
    }
    menu_videos = collect_course_menu_videos(driver)
    extra_videos = [
        video for video in menu_videos
        if video["title"] not in task_video_titles
    ]
    return {
        "cid": cid,
        "url": course_url,
        "title": title,
        "cached_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": tasks,
        "extra_videos": extra_videos,
    }


def refresh_task_cache(ctx: PluginContext, driver,
                       courses: list[tuple[str, str]]) -> dict:
    cache = load_task_cache(ctx)
    errors = 0
    for index, (course_url, cid) in enumerate(courses, start=1):
        ctx.progress(
            stage="cache", done=index - 1, total=len(courses),
            title=f"读取任务缓存: {cid}")
        try:
            course = cache_course_tasks(ctx, driver, course_url, cid)
            cache["courses"][cid] = course
            cache["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_task_cache(ctx, cache)
            ctx.log(
                f"已缓存课程 {course['title']}: "
                f"{len(course['tasks'])} 个任务, "
                f"{len(course['extra_videos'])} 个任务外视频")
        except (RuntimeError, WebDriverException) as exc:
            errors += 1
            ctx.log(f"课程任务缓存失败({cid}): {exc}")
    ctx.progress(
        stage="cache", done=len(courses), total=len(courses),
        title="任务缓存完成")
    if errors == len(courses):
        raise RuntimeError("全部课程任务缓存失败")
    return cache


def _wait_task_dialog_closed(driver, timeout: float = 1.8) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _task_pane_ready(driver, require_cards=False):
            return True
        time.sleep(0.1)
    return False


def _task_dialog_element(driver):
    panes = driver.find_elements(By.ID, "pane-first")
    if not panes:
        return None
    return driver.execute_script(
        "return arguments[0].closest('.el-dialog');", panes[0])


def _task_close_button(driver) -> bool:
    dialog = _task_dialog_element(driver)
    if dialog is None:
        return False
    buttons = dialog.find_elements(By.CSS_SELECTOR, ".el-dialog__headerbtn")
    for button in buttons:
        if not button.is_displayed():
            continue
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", button)
            button.click()
        except WebDriverException:
            try:
                driver.execute_script("arguments[0].click();", button)
            except WebDriverException:
                continue
        return True
    return False


def _task_overlay_native_click(driver) -> bool:
    dialog = _task_dialog_element(driver)
    if dialog is None:
        return False
    overlay = driver.execute_script(
        "return arguments[0].closest('.el-overlay');", dialog)
    if overlay is None:
        return False
    size = overlay.size
    try:
        ActionChains(driver).move_to_element(overlay).move_by_offset(
            -(size.get("width", 0) // 2) + 10,
            -(size.get("height", 0) // 2) + 10,
        ).click().perform()
        return True
    except WebDriverException:
        return False


def _task_overlay_js_click(driver) -> bool:
    return bool(driver.execute_script(
        """
        const pane = document.getElementById('pane-first');
        const dialog = pane && pane.closest('.el-dialog');
        const overlay = dialog && dialog.closest('.el-overlay');
        const target = dialog && dialog.closest('.el-overlay-dialog');
        if (!overlay || !target) return false;
        const rect = dialog.getBoundingClientRect();
        const x = Math.max(2, Math.min(rect.left - 4, window.innerWidth - 2));
        const y = Math.max(2, Math.min(rect.top - 4, window.innerHeight - 2));
        for (const type of ['mousedown', 'mouseup', 'click']) {
            target.dispatchEvent(new MouseEvent(type, {
                bubbles: true, cancelable: true, clientX: x, clientY: y
            }));
        }
        return true;
        """
    ))


def _task_dialog_escape(driver) -> bool:
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        return True
    except WebDriverException:
        return False


def close_task_dialog(driver, reload_on_failure: bool = False) -> None:
    """Close only the task dialog that contains ``#pane-first``."""
    if not _task_pane_ready(driver, require_cards=False):
        return
    actions = (
        _task_close_button,
        _task_overlay_native_click,
        _task_dialog_escape,
        _task_overlay_js_click,
    )
    for _ in range(2):
        for action in actions:
            try:
                triggered = action(driver)
            except WebDriverException:
                triggered = False
            if triggered and _wait_task_dialog_closed(driver):
                return
        time.sleep(0.3)
    if reload_on_failure:
        try:
            driver.get(driver.current_url)
            if _wait_task_dialog_closed(driver, timeout=8):
                return
        except WebDriverException:
            pass
    raise RuntimeError(
        "任务进度窗口未能关闭（已尝试关闭按钮、遮罩点击和 Esc）")


def read_quiz_fields(driver, survey_strategy: str = "fixed",
                     survey_positive_value: object = 3,
                     survey_negative_value: object = 1) -> list[dict]:
    deadline = time.time() + 25
    rows: list[dict] = []
    while time.time() < deadline:
        rows = driver.execute_script(
            """
            const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
            return Array.from(document.querySelectorAll('[id^="quz"]')).map(row => {
                const card = row.closest('.el-card') || row.parentElement;
                const radios = Array.from(card.querySelectorAll('input[type=radio]'));
                const checks = Array.from(card.querySelectorAll('input[type=checkbox]'));
                const options = (checks.length ? checks : radios).map(input => ({
                    value: input.value || '',
                    label: norm(
                        (input.closest('.el-radio, .el-checkbox') || input.parentElement)
                            .innerText || ''
                    ),
                }));
                return {
                    id: row.id,
                    text: norm(row.innerText),
                    cardText: norm(card.innerText),
                    radioGroups: Array.from(new Set(radios.map(el => el.name))).length,
                    radios: radios.length,
                    checks: checks.length,
                    textareas: card.querySelectorAll('textarea').length,
                    options,
                };
            });
            """
        )
        if rows:
            break
        time.sleep(0.5)
    if not rows:
        raise RuntimeError("未识别到答题界面")

    strategy = str(survey_strategy or "fixed").strip().lower()
    if strategy not in {"fixed", "normal"}:
        strategy = "fixed"
    positive_value = survey_choice(survey_positive_value, 3)
    negative_value = survey_choice(survey_negative_value, 1)
    fields: list[dict] = []
    for row in rows:
        qid = str(row.get("id") or "")
        text = normalize_title(row.get("text"))
        radios = int(row.get("radios") or 0)
        checks = int(row.get("checks") or 0)
        radio_groups = int(row.get("radioGroups") or 0)
        card_text = normalize_title(row.get("cardText"))
        if (radio_groups >= 5 and "非常不同意" in card_text
                and "非常同意" in card_text):
            for index, (label, valence) in enumerate(
                    zip(SURVEY_QUESTIONS, SURVEY_VALENCES, strict=True)):
                value = survey_normal_value() if strategy == "normal" else (
                    positive_value if valence > 0 else negative_value)
                fields.append({
                    "key": f"{qid}_survey_{index}",
                    "label": f"量表 {index + 1}：{label}",
                    "type": "scale",
                    "options": [
                        {"value": option_value, "label": label_}
                        for option_value, label_ in SURVEY_OPTIONS
                    ],
                    "value": value,
                    "readonly": True,
                    "strategy": strategy,
                    "control_id": qid,
                    "group_index": index,
                })
            continue
        if "单选题" in text and radios:
            fields.append({
                "key": qid,
                "label": re.sub(
                    r"\s*单选题（\d+分）\s*$", "", text).strip(),
                "type": "single",
                "options": row.get("options") or [],
                "value": "",
            })
        elif "多选题" in text and checks:
            fields.append({
                "key": qid,
                "label": re.sub(
                    r"\s*多选题（\d+分）\s*$", "", text).strip(),
                "type": "multiple",
                "options": row.get("options") or [],
                "value": [],
            })
        elif "简答题" in text and int(row.get("textareas") or 0):
            fields.append({
                "key": qid,
                "label": re.sub(
                    r"\s*简答题（\d+分）\s*$", "", text).strip(),
                "type": "short",
                "value": "",
            })
    if not fields:
        raise RuntimeError("答题界面中没有可处理的题目")
    return fields


def _quiz_value(fields: list[dict], key: str) -> object:
    for field in fields:
        if field.get("key") == key:
            return field.get("value")
    return None


def fill_quiz(driver, fields: list[dict]) -> None:
    for field in fields:
        kind = field.get("type")
        value = field.get("value")
        if kind == "scale":
            ok = driver.execute_script(
                """
                const row = document.getElementById(arguments[0]);
                const card = row && (row.closest('.el-card') || row.parentElement);
                if (!card) return false;
                const groups = [];
                for (const input of card.querySelectorAll('input[type=radio]')) {
                    let group = groups.find(g => g.name === input.name);
                    if (!group) {
                        group = {name: input.name, inputs: []};
                        groups.push(group);
                    }
                    group.inputs.push(input);
                }
                const group = groups[arguments[1]];
                if (!group) return false;
                const input = group.inputs.find(el => el.value === String(arguments[2]));
                if (!input) return false;
                input.click();
                return true;
                """,
                field.get("control_id"),
                field.get("group_index"),
                str(value),
            )
            if not ok:
                raise RuntimeError(f"量表选项设置失败: {field.get('label')}")
        elif kind in {"single", "multiple"}:
            values = value if isinstance(value, list) else [value]
            selector = "radio" if kind == "single" else "checkbox"
            wanted = {str(item) for item in values}
            options = [
                str(option.get("value"))
                for option in field.get("options", [])
                if option.get("value") is not None
            ]
            targets = options if kind == "multiple" else list(wanted)
            for target in targets:
                should = target in wanted
                if kind == "single" and not should:
                    continue
                found = driver.execute_script(
                    """
                    const row = document.getElementById(arguments[0]);
                    const card = row && (row.closest('.el-card') || row.parentElement);
                    if (!card) return false;
                    const input = Array.from(card.querySelectorAll(
                        'input[type=' + arguments[3] + ']'))
                        .find(el => el.value === String(arguments[1]));
                    if (!input) return false;
                    if (input.checked !== Boolean(arguments[2])) {
                        const control = input.closest(
                            arguments[3] === 'radio' ? '.el-radio' : '.el-checkbox');
                        (control || input).click();
                    }
                    return true;
                    """,
                    field.get("key"), target, should, selector,
                )
                if not found:
                    raise RuntimeError(
                        f"选项设置失败: {field.get('label')} / {target}")
                time.sleep(0.12)
                checked = driver.execute_script(
                    """
                    const row = document.getElementById(arguments[0]);
                    const card = row && (row.closest('.el-card') || row.parentElement);
                    if (!card) return false;
                    const input = Array.from(card.querySelectorAll(
                        'input[type=' + arguments[3] + ']'))
                        .find(el => el.value === String(arguments[1]));
                    return !!input && input.checked === Boolean(arguments[2]);
                    """,
                    field.get("key"), target, should, selector,
                )
                if not checked:
                    raise RuntimeError(
                        f"选项未生效: {field.get('label')} / {target}")
            if not wanted:
                raise RuntimeError(f"选项设置失败: {field.get('label')}")
        elif kind == "short":
            text = str(value or "").strip()
            if not text:
                raise RuntimeError(f"简答题答案为空: {field.get('label')}")
            row = driver.find_element(By.ID, field["key"])
            card = driver.execute_script(
                "return arguments[0].closest('.el-card') || arguments[0].parentElement;",
                row,
            )
            textareas = card.find_elements(By.CSS_SELECTOR, "textarea")
            visible = [el for el in textareas if el.is_displayed()]
            if not visible:
                raise RuntimeError(f"未找到简答题输入框: {field.get('label')}")
            element = visible[0]
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});"
                "arguments[0].focus();", element)
            element.clear()
            element.send_keys(text)


def _click_visible_button(driver, text: str, scope: str = "") -> bool:
    return bool(driver.execute_script(
        """
        const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
        const visible = el =>
            !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const root = arguments[1] ? document.querySelector(arguments[1]) : document;
        if (!root) return false;
        const buttons = Array.from(root.querySelectorAll('button'))
            .filter(el => visible(el) && norm(el.innerText) === arguments[0]);
        if (!buttons.length) return false;
        buttons[buttons.length - 1].scrollIntoView({block: 'center'});
        buttons[buttons.length - 1].click();
        return true;
        """,
        text,
        scope,
    ))


def submission_tag_colors(driver) -> list[str]:
    colors = driver.execute_script(
        """
        const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
        return Array.from(document.querySelectorAll('span, div'))
            .filter(el => norm(el.innerText) === '提交问卷')
            .map(el => el.style.color || getComputedStyle(el).color)
            .filter(Boolean);
        """
    )
    return [str(c).lower() for c in (colors or [])]


def _quiz_marked_submitted(driver, test_title: str) -> bool:
    colors = submission_tag_colors(driver)
    if any("67c23a" in color or "103, 194, 58" in color for color in colors):
        return True
    try:
        groups = collect_task_groups(driver)
    except Exception:
        return False
    for group in groups:
        for item in group.get("items", []):
            if normalize_title(item.get("title")) == normalize_title(test_title):
                return "已提交" in item.get("tags", [])
    return False


def submit_quiz(driver, test_title: str) -> bool:
    button = find(driver, QUIZ_SUBMIT_XPATH, timeout=10)
    if button is None:
        button = find(
            driver,
            '//button[normalize-space(.)="提交" or normalize-space(.)="提交试卷"]',
            timeout=5,
        )
    if button is None:
        raise RuntimeError("未找到测验提交按钮")
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});", button)
    try:
        button.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", button)

    deadline = time.time() + 10
    while time.time() < deadline:
        clicked = driver.execute_script(
            """
            const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
            const visible = el =>
                !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const boxes = Array.from(document.querySelectorAll('.el-message-box'))
                .filter(visible);
            if (!boxes.length) return false;
            const buttons = Array.from(boxes[boxes.length - 1].querySelectorAll('button'))
                .filter(visible);
            const target = buttons.find(el => ['确定', '确认'].includes(norm(el.innerText)))
                || buttons[buttons.length - 1];
            if (!target) return false;
            target.click();
            return true;
            """
        )
        if clicked:
            break
        time.sleep(0.3)

    deadline = time.time() + 30
    while time.time() < deadline:
        if _quiz_marked_submitted(driver, test_title):
            return True
        time.sleep(1.0)
    return False


def read_comment_cards(driver) -> list[dict]:
    cards = driver.execute_script(
        """
        const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
        const bar = document.getElementById('commentBarScroll');
        if (!bar) return [];
        return Array.from(bar.querySelectorAll(':scope > ul > li')).map((li, index) => ({
            index,
            user: norm((li.querySelector('.nameRow .name') || {}).innerText || ''),
            text: norm((li.querySelector('.nameRow .text') || {}).innerText || ''),
        })).filter(item => item.text);
        """
    )
    return cards if isinstance(cards, list) else []


def _visible_editor(driver):
    editors = driver.find_elements(By.CSS_SELECTOR, "textarea, [contenteditable=true]")
    visible = [el for el in editors if el.is_displayed()]
    return visible[-1] if visible else None


def _enter_editor_text(driver, text: str) -> None:
    editor = _visible_editor(driver)
    if editor is None:
        raise RuntimeError("评论编辑器未出现")
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});"
        "arguments[0].focus();", editor)
    editor.clear()
    editor.send_keys(text)


def post_comment(driver, text: str) -> None:
    if not _click_visible_button(driver, "发表评论"):
        raise RuntimeError("未找到“发表评论”按钮")
    deadline = time.time() + 5
    while time.time() < deadline:
        if _visible_editor(driver) is not None:
            break
        time.sleep(0.2)
    _enter_editor_text(driver, text)
    if not _click_visible_button(driver, "确认"):
        raise RuntimeError("未找到评论确认按钮")
    time.sleep(1.5)


def _first_comment_element(driver):
    comments = driver.find_elements(
        By.XPATH, '//*[@id="commentBarScroll"]/ul/li[1]')
    if not comments:
        raise RuntimeError("未找到第一条评论")
    return comments[0]


def _click_scoped_live_button(driver, scope, relative_xpath: str,
                              text: str) -> bool:
    buttons = scope.find_elements(By.XPATH, relative_xpath)
    if not buttons:
        buttons = scope.find_elements(
            By.XPATH,
            f'.//button[normalize-space(.)="{text}"]',
        )
    for button in buttons:
        if not button.is_displayed():
            continue
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", button)
            button.click()
        except WebDriverException:
            try:
                driver.execute_script("arguments[0].click();", button)
            except WebDriverException:
                continue
        return True
    return False


def _open_first_comment_reply(driver) -> None:
    first = _first_comment_element(driver)
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});", first)
    if not _click_scoped_live_button(
            driver, first,
            './div/div/div/div[3]/button[2]', "回复"):
        raise RuntimeError("未找到第一条评论的回复按钮")


def _reply_editor(driver):
    deadline = time.time() + 5
    while time.time() < deadline:
        first = _first_comment_element(driver)
        editors = first.find_elements(
            By.XPATH,
            './div/div/div/div[4]/div[2]/li/div/div/div[1]/textarea',
        )
        if not editors:
            editors = first.find_elements(By.XPATH, './/textarea')
        for editor in editors:
            if editor.is_displayed():
                return editor
        time.sleep(0.2)
    raise RuntimeError("第一条评论的回复输入框未出现")


def _enter_reply_text(driver, text: str) -> None:
    editor = _reply_editor(driver)
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});"
        "arguments[0].focus();", editor)
    editor.clear()
    editor.send_keys(text)
    if str(editor.get_attribute("value") or "").strip() != text.strip():
        raise RuntimeError("回复文字未写入第一条评论的回复框")


def _submit_first_comment_reply(driver) -> None:
    first = _first_comment_element(driver)
    if not _click_scoped_live_button(
            driver, first,
            './div/div/div/div[4]/div[2]/li/div/button[2]', "确认"):
        raise RuntimeError("未找到回复确认按钮")
    time.sleep(1.5)


def post_reply(driver, text: str) -> None:
    _open_first_comment_reply(driver)
    _enter_reply_text(driver, text)
    _submit_first_comment_reply(driver)


def _login(ctx: PluginContext, driver, course_url: str) -> None:
    driver.get(course_url)
    login_button = find(driver, LOGIN_BUTTON_XPATH, timeout=12)
    if login_button is None:
        raise RuntimeError("未找到北师大统一身份认证登录入口")
    try:
        login_button.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", login_button)

    user_input = find_strict(driver, USERNAME_XPATH, timeout=30)
    password_input = find_strict(driver, PASSWORD_XPATH, timeout=15)
    user_input.clear()
    user_input.send_keys(ctx.username)
    password_input.clear()
    password_input.send_keys(ctx.password)
    submit = find_strict(driver, SUBMIT_XPATH, timeout=10)
    try:
        submit.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", submit)

    deadline = time.time() + 60
    while time.time() < deadline:
        current = driver.current_url
        if "cas.bnu.edu.cn" not in current and "bdata.bnu.edu.cn" in current:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("统一身份认证登录超时, 请检查账号密码")


def ensure_authenticated(ctx: PluginContext, driver, course_url: str,
                         cid: str) -> None:
    cookies = load_cookies(ctx)
    if cookies:
        inject_cookies(driver, cookies)
        driver.get(course_url)
        time.sleep(1.5)
        try:
            course_data(driver, cid)
            ctx.log("使用缓存 Cookie 登录师道云")
            return
        except RuntimeError:
            ctx.log("缓存 Cookie 已失效, 重新执行统一身份认证")

    ctx.status("正在登录北师大统一身份认证…")
    _login(ctx, driver, course_url)
    driver.get(course_url)
    course_data(driver, cid)
    save_cookies(ctx, driver.get_cookies())
    ctx.log("统一身份认证登录成功, Cookie 已缓存")


def click_menu_title(driver, title: str) -> bool:
    js = """
    const target = String(arguments[0]).replace(/\\s+/g, ' ').trim();
    const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
    const item = Array.from(document.querySelectorAll('li.el-menu-item'))
        .find(el => norm(el.innerText) === target);
    if (!item) return false;
    item.scrollIntoView({block: 'center'});
    item.click();
    return true;
    """
    return bool(driver.execute_script(js, title))


def wait_active_menu_title(driver, title: str, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    target = normalize_title(title)
    while time.time() < deadline:
        try:
            active = driver.execute_script(
                "const el=document.querySelector('li.el-menu-item.is-active');"
                "return el ? el.innerText : '';"
            )
            if normalize_title(active) == target:
                return
        except WebDriverException:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"切换课程内容超时: {title}")


def duration_text(driver) -> str:
    js = """
    const xpath = arguments[0];
    const el = document.evaluate(xpath, document, null,
        XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
    if (el && (el.innerText || '').trim()) return el.innerText.trim();
    const tag = Array.from(document.querySelectorAll('.el-tag__content'))
        .map(el => (el.innerText || '').trim())
        .find(text => /观看视频|视频时长|学习时长|已完成/.test(text));
    return tag || '';
    """
    return normalize_title(driver.execute_script(js, DURATION_XPATH))


def wait_duration_label(driver, timeout: float = 6.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            last = duration_text(driver)
        except WebDriverException:
            last = ""
        if last and (parse_duration_seconds(last) > 0 or "已完成" in last):
            return last
        time.sleep(0.4)
    return last


def video_state(driver) -> dict:
    js = """
    const v = document.querySelector('#player video, video');
    if (!v) return null;
    return {
        currentTime: Number(v.currentTime || 0),
        duration: Number(v.duration || 0),
        paused: Boolean(v.paused),
        ended: Boolean(v.ended),
        rate: Number(v.playbackRate || 1)
    };
    """
    state = driver.execute_script(js)
    return state if isinstance(state, dict) else {}


def start_or_resume_video(driver, watched: float, speedup: float) -> None:
    poster = find(driver, "#player .nplayer_poster", timeout=3,
                  by=By.CSS_SELECTOR)
    if poster is not None:
        try:
            poster.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", poster)
    state = video_state(driver)
    if state and watched > 1 and state.get("currentTime", 0) < watched - 0.5:
        seek_to = min(float(watched), max(0.0, state.get("duration", watched) - 0.5))
        try:
            driver.execute_script(
                "arguments[0].currentTime = arguments[1];",
                driver.find_element(By.CSS_SELECTOR, "#player video, video"),
                seek_to,
            )
        except WebDriverException:
            pass
    driver.execute_script(
        """
        const v = document.querySelector('#player video, video');
        if (!v) return false;
        v.muted = true;
        v.playbackRate = arguments[0];
        v.play().catch(() => {});
        return true;
        """,
        speedup,
    )


def restart_video_from_start(driver, speedup: float) -> bool:
    """Rewind when the local player ended but the server is still incomplete."""
    try:
        return bool(driver.execute_script(
            """
            const v = document.querySelector('#player video, video');
            if (!v || !(v.duration > 0)) return false;
            try {
                v.pause();
                v.currentTime = 0;
            } catch (e) {
                return false;
            }
            v.muted = true;
            v.playbackRate = arguments[0];
            const played = v.play();
            if (played && played.catch) played.catch(() => {});
            return true;
            """,
            speedup,
        ))
    except WebDriverException:
        return False


def wait_video_duration(driver, timeout: float = 20.0) -> float:
    deadline = time.time() + timeout
    while time.time() < deadline:
        duration = float(video_state(driver).get("duration") or 0)
        if duration > 0:
            return duration
        time.sleep(0.4)
    return 0.0


def play_until_complete(driver, title: str, media_duration: float,
                        watched: float, speedup: float, tolerance: float,
                        on_progress=None) -> tuple[bool, float, float]:
    deadline_wait = time.time() + 20
    while time.time() < deadline_wait:
        media_duration = float(video_state(driver).get("duration") or 0)
        if media_duration > 0:
            break
        time.sleep(0.4)
    if media_duration <= 0:
        return False, watched, 0.0

    if is_complete(watched, media_duration, tolerance):
        return True, watched, media_duration

    start_or_resume_video(driver, watched, speedup)
    started = time.time()
    max_wait = max(
        180.0, (media_duration - viewed(media_duration, watched)) / max(
            1.0, speedup) * 2.2 + 120.0)
    last_report = 0.0
    last_media = -1.0
    stalled_rounds = 0
    server_watched = watched
    restart_count = 0
    next_restart_at = 0.0

    while time.time() - started < max_wait:
        state = video_state(driver)
        current = float(state.get("currentTime") or 0)
        media_duration = float(state.get("duration") or media_duration)
        if state.get("paused") and not state.get("ended"):
            driver.execute_script(
                """
                const v = document.querySelector('#player video, video');
                if (v) {
                    v.muted = true;
                    v.playbackRate = arguments[0];
                    v.play().catch(() => {});
                }
                """,
                speedup,
            )

        now = time.time()
        try:
            label = duration_text(driver)
            if "已完成" in label:
                server_watched = media_duration
            else:
                parsed = parse_duration_seconds(label)
                if parsed > 0 or label:
                    server_watched = float(parsed)
        except WebDriverException:
            pass

        if is_complete(server_watched, media_duration, tolerance):
            driver.execute_script(
                "const v=document.querySelector('#player video, video');"
                "if(v){v.pause();}")
            return True, server_watched, media_duration

        if now - last_report >= 2:
            last_report = now
            percent = min(100.0, server_watched / media_duration * 100)
            remain = max(0.0, media_duration - server_watched) / max(1.0, speedup)
            if on_progress:
                on_progress({
                    "title": title,
                    "watched_seconds": round(server_watched, 1),
                    "target_seconds": round(media_duration, 1),
                    "percent": round(percent, 1),
                    "eta_s": round(remain),
                    "replays": restart_count,
                })

        # A previously watched local video may already be at its end while the
        # server still has incomplete time. The player cannot progress from
        # that state, so replay from zero until server completion.
        if (state.get("ended") and now >= next_restart_at
                and restart_video_from_start(driver, speedup)):
            restart_count += 1
            next_restart_at = now + 2.0
            last_media = -1.0
            stalled_rounds = 0
            if on_progress:
                on_progress({
                    "title": title,
                    "watched_seconds": round(server_watched, 1),
                    "target_seconds": round(media_duration, 1),
                    "percent": round(server_watched / media_duration * 100, 1),
                    "eta_s": round(max(
                        0.0, media_duration - server_watched) / max(1.0, speedup)),
                    "replays": restart_count,
                    "replaying": True,
                })
            time.sleep(0.5)
            continue
        if current <= last_media + 0.05:
            stalled_rounds += 1
        else:
            stalled_rounds = 0
            last_media = current
        if stalled_rounds >= 15 and not state.get("ended"):
            start_or_resume_video(driver, server_watched, speedup)
            stalled_rounds = 0
        time.sleep(1.0)

    try:
        label = duration_text(driver)
        if "已完成" in label:
            server_watched = media_duration
        else:
            server_watched = float(parse_duration_seconds(label))
    except WebDriverException:
        pass
    return is_complete(server_watched, media_duration, tolerance), \
        server_watched, media_duration


def viewed(media_duration: float, watched: float) -> float:
    """Clamp server time for max-wait calculation only."""
    return min(max(0.0, media_duration), max(0.0, watched))


class PlayTracker:
    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.lock = threading.Lock()
        self.workers: dict[int, dict] = {}
        self.results: dict[str, str] = {}

    def set_worker(self, worker_id: int, **fields) -> None:
        with self.lock:
            self.workers.setdefault(worker_id, {}).update(fields)

    def mark(self, job_key: str, status: str) -> None:
        with self.lock:
            previous = self.results.get(job_key)
            self.results[job_key] = status
            if status == "已完成" and previous != "已完成":
                self.done += 1
            elif previous == "已完成" and status != "已完成":
                self.done -= 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "done": self.done,
                "total": self.total,
                "workers": {str(k): dict(v) for k, v in self.workers.items()},
            }


def discover_course_jobs(ctx: PluginContext, driver,
                         courses: list[tuple[str, str]]) -> list[dict]:
    jobs: list[dict] = []
    for course_index, (course_url, cid) in enumerate(courses, start=1):
        driver.get(course_url)
        data = course_data(driver, cid)
        course_title = normalize_title(data.get("title")) or cid
        videos = collect_task_videos(driver)
        ctx.log(
            f"[{course_index}/{len(courses)}] {course_title}: "
            f"任务中共有 {len(videos)} 个必修视频")
        for index, video in enumerate(videos, start=1):
            title = video["title"]
            jobs.append({
                "key": f"{cid}:{index}:{title}",
                "title": title,
                "course_url": course_url,
                "cid": cid,
                "course_title": course_title,
            })
    return jobs


def run_worker(ctx: PluginContext, worker_id: int, jobs: queue.Queue,
               cookies: list[dict], tracker: PlayTracker,
               first_course: tuple[str, str], speedup: float,
               tolerance: float, only_unfinished: bool,
               initial_driver=None) -> None:
    driver = initial_driver
    current_course = ""
    try:
        if driver is None:
            driver = make_driver(ctx, headless=bool(ctx.cfg("headless", True)))
            inject_cookies(driver, cookies)
            ensure_authenticated(ctx, driver, first_course[0], first_course[1])

        while True:
            try:
                job = jobs.get_nowait()
            except queue.Empty:
                tracker.set_worker(
                    worker_id, status="空闲", title="", percent=None,
                    eta_s=None, total_s=None)
                return
            title = job["title"]
            course_title = job["course_title"]
            tracker.set_worker(
                worker_id, status="检查中", title=title, percent=0.0,
                eta_s=None, total_s=None, course=course_title)
            try:
                if current_course != job["course_url"]:
                    driver.get(job["course_url"])
                    current_course = job["course_url"]
                    time.sleep(1.0)
                if not click_menu_title(driver, title):
                    raise RuntimeError("课程目录中未找到该视频")
                wait_active_menu_title(driver, title)
                media_duration = wait_video_duration(driver)
                if media_duration <= 0:
                    raise RuntimeError("视频时长不可用")
                page_label = wait_duration_label(driver)
                watched = float(parse_duration_seconds(page_label))
                if only_unfinished and (
                        "已完成" in page_label
                        or is_complete(watched, media_duration, tolerance)):
                    tracker.mark(job["key"], "已完成")
                    tracker.set_worker(
                        worker_id, status="已完成", title=title,
                        percent=100.0, eta_s=0, total_s=round(media_duration))
                    ctx.log(
                        f"[w{worker_id}] 跳过已完成 "
                        f"{watched:.0f}/{media_duration:.0f}s: {title}")
                    continue

                def report(fields: dict, course_title: str = course_title) -> None:
                    tracker.set_worker(
                        worker_id,
                        status="重播中" if fields.get("replaying") else "播放中",
                        title=fields["title"],
                        percent=fields["percent"],
                        eta_s=fields["eta_s"],
                        total_s=round(fields["target_seconds"]),
                        course=course_title,
                        replays=fields.get("replays", 0),
                    )

                ok, watched, media_duration = play_until_complete(
                    driver, title, media_duration, watched, speedup,
                    tolerance, on_progress=report)
                if not ok:
                    raise RuntimeError(
                        f"服务器录制进度未达到视频时长"
                        f"({watched:.0f}/{media_duration:.0f}s)")
                tracker.mark(job["key"], "已完成")
                tracker.set_worker(
                    worker_id, status="已完成", title=title, percent=100.0,
                    eta_s=0, total_s=round(media_duration))
                ctx.log(
                    f"[w{worker_id}] 完成 "
                    f"{watched:.0f}/{media_duration:.0f}s: {title}")
            except Exception as e:
                tracker.mark(job["key"], "出错")
                tracker.set_worker(
                    worker_id, status="出错", title=title, percent=None,
                    eta_s=None, total_s=None)
                ctx.log(f"[w{worker_id}] 视频失败: {title}: {e!r}")
            finally:
                jobs.task_done()
    except Exception as e:
        tracker.set_worker(worker_id, status="崩溃", title="")
        ctx.log(f"[w{worker_id}] 工作进程崩溃: {e!r}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        tracker.set_worker(worker_id, status="已关闭")


def run_parallel(ctx: PluginContext, jobs: list[dict], cookies: list[dict],
                 initial_driver, speedup: float, tolerance: float,
                 only_unfinished: bool, worker_count: int) -> PlayTracker:
    tracker = PlayTracker(len(jobs))
    work: queue.Queue = queue.Queue()
    for job in jobs:
        work.put(job)
    first_course = (jobs[0]["course_url"], jobs[0]["cid"])
    threads = []
    for worker_id in range(min(worker_count, len(jobs))):
        thread = threading.Thread(
            target=run_worker,
            args=(
                ctx, worker_id, work, cookies, tracker, first_course,
                speedup, tolerance, only_unfinished,
                initial_driver if worker_id == 0 else None,
            ),
            daemon=True,
            name=f"bdata-worker-{worker_id}",
        )
        thread.start()
        threads.append(thread)
        time.sleep(0.3)

    ctx.progress(**tracker.snapshot())
    while any(thread.is_alive() for thread in threads):
        ctx.progress(**tracker.snapshot())
        time.sleep(1.0)
    ctx.progress(**tracker.snapshot())
    return tracker


def _reviewed_fields(fields: list[dict], response: dict | None) -> list[dict]:
    if not response or response.get("action") != "approve":
        return fields
    payload = response.get("payload")
    raw_fields = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(raw_fields, list):
        return fields
    reviewed = {
        str(item.get("key")): item.get("value")
        for item in raw_fields if isinstance(item, dict) and item.get("key")
    }
    for field in fields:
        if not field.get("readonly") and field.get("key") in reviewed:
            field["value"] = reviewed[field["key"]]
    return fields


def _metric_value(metrics: list[str], name: str) -> tuple[int, int]:
    pattern = re.compile(rf"^{re.escape(name)}(\d+)/(\d+)$")
    for metric in metrics:
        match = pattern.match(normalize_title(metric))
        if match:
            return int(match.group(1)), int(match.group(2))
    return 0, 0


def run_quiz_item(ctx: PluginContext, driver, broker: ReviewBroker,
                  course_title: str, test_title: str,
                  model: tuple[str, str, str],
                  full_auto: bool = False) -> bool:
    open_task_dialog(driver)
    click_task_entry(driver, test_title)
    fields = read_quiz_fields(
        driver,
        survey_strategy=str(ctx.cfg("survey_strategy", "fixed")),
        survey_positive_value=ctx.cfg("survey_positive_value", 3),
        survey_negative_value=ctx.cfg("survey_negative_value", 1),
    )
    model_fields = [field for field in fields if field.get("type") != "scale"]
    review_fields = [field for field in fields if not field.get("readonly")]
    ctx.status(f"正在生成答案: {test_title}")
    answers = generate_quiz_answers(
        *model, course_title, test_title, model_fields)
    for field in model_fields:
        answer = answers.get(field["key"], {})
        field["value"] = answer.get(
            "selected", "") if field["type"] == "single" else answer.get(
            "selected", []) if field["type"] == "multiple" else answer.get(
            "text", "")
    ctx.log(f"模型已生成 {test_title} 的答案")
    if full_auto:
        ctx.log(f"全自动模式：跳过人工核验并提交 {test_title}")
        response = {"action": "approve", "payload": {"fields": []}}
    else:
        response = broker.ask("quiz", f"测验：{test_title}", review_fields)
    if not response or response.get("action") == "skip":
        ctx.log(f"用户跳过测验: {test_title}")
        return False
    reviewed = _reviewed_fields(fields, response)
    fill_quiz(driver, reviewed)
    ctx.status(f"正在提交测验: {test_title}")
    if not submit_quiz(driver, test_title):
        raise RuntimeError("提交后未检测到已提交状态或成功颜色")
    ctx.log(f"测验已提交: {test_title}")
    return True


def run_interaction_group(ctx: PluginContext, driver, broker: ReviewBroker,
                          course_url: str, course_title: str, group: dict,
                          model: tuple[str, str, str],
                          full_auto: bool = False) -> bool:
    videos = [
        item for item in group.get("items", [])
        if item.get("icon", "").startswith(TASK_VIDEO_ICON_PREFIX)
    ]
    if not videos:
        ctx.log(f"互动任务没有可打开的视频，跳过: {group.get('header')}")
        return False
    video_title = normalize_title(videos[0].get("title"))
    need_comment = task_group_has(group, "评论")
    need_reply = task_group_has(group, "回复")
    if not need_comment and not need_reply:
        return False

    driver.get(course_url)
    open_task_dialog(driver)
    click_task_entry(driver, video_title)
    time.sleep(1.5)
    comments = read_comment_cards(driver)
    texts = [normalize_title(card.get("text")) for card in comments if card.get("text")]
    if not texts:
        raise RuntimeError("评论区没有可用于参考的评论")
    sample = random.sample(texts, min(5, len(texts)))
    ctx.status(f"正在生成评论与回复: {video_title}")
    generated = generate_interaction(*model, course_title, video_title, sample)
    fields = []
    if need_comment:
        fields.append({
            "key": "comment",
            "label": "课程评论（约100字）",
            "type": "text",
            "value": generated["comment"],
        })
    if need_reply:
        fields.append({
            "key": "reply",
            "label": "对第一条评论的回复（约20字）",
            "type": "text",
            "value": generated["reply"],
        })
    context = [f"参考评论 {i + 1}：{text}" for i, text in enumerate(sample)]
    ctx.log(f"模型已生成 {video_title} 的互动内容")
    if full_auto:
        ctx.log(f"全自动模式：跳过人工核验并提交 {video_title} 的评论与回复")
        response = {"action": "approve", "payload": {"fields": []}}
    else:
        response = broker.ask(
            "interaction", f"评论与回复：{video_title}", fields, context)
    if not response or response.get("action") == "skip":
        ctx.log(f"用户跳过互动任务: {group.get('header')}")
        return False
    reviewed = _reviewed_fields(fields, response)
    values = {str(field["key"]): str(field.get("value") or "").strip()
              for field in reviewed}
    if need_comment:
        if not values.get("comment"):
            raise RuntimeError("评论内容为空")
        post_comment(driver, values["comment"])
    if need_reply:
        if not values.get("reply"):
            raise RuntimeError("回复内容为空")
        post_reply(driver, values["reply"])
    ctx.log(f"评论与回复已提交: {video_title}")
    return True


def run_qa(ctx: PluginContext, driver,
           courses: list[tuple[str, str]]) -> None:
    api_url = str(
        ctx.cfg("api_url", "https://api.deepseek.com")
        or "https://api.deepseek.com").strip()
    api_key = str(ctx.cfg("api_key") or "").strip()
    model_id = str(
        ctx.cfg("model_id", "deepseek-flash") or "deepseek-flash").strip()
    if not (api_url and api_key and model_id):
        raise RuntimeError("答题模式需要填写模型 API 地址、API Key 和模型 ID")
    model = (api_url, api_key, model_id)
    broker = ReviewBroker(ctx)
    broker.clear()
    try:
        work: list[tuple[tuple[str, str], str, dict | str]] = []
        for course in courses:
            course_url, cid = course
            driver.get(course_url)
            title = normalize_title(course_data(driver, cid).get("title")) or cid
            groups = collect_task_groups(driver)
            for group in groups:
                for item in group.get("items", []):
                    if (item.get("icon", "").startswith(TASK_TEST_ICON_PREFIX)
                            and "尚未提交" in item.get("tags", [])):
                        work.append((course, title, item["title"]))
                if (task_group_has(group, "评论")
                        or task_group_has(group, "回复")):
                    work.append((course, title, group))

        if not work:
            ctx.progress(done=0, total=0)
            ctx.status("没有待处理的测验或互动任务", done=True)
            return
        ctx.log(f"答题与互动任务共 {len(work)} 项")
        done = 0
        skipped = 0
        for course, course_title, work_item in work:
            course_url, _cid = course
            driver.get(course_url)
            try:
                if isinstance(work_item, str):
                    ctx.progress(
                        done=done, total=len(work), course=course_title,
                        title=work_item)
                    succeeded = run_quiz_item(
                        ctx, driver, broker, course_title, work_item, model)
                else:
                    ctx.progress(
                        done=done, total=len(work), course=course_title,
                        title=normalize_title(work_item.get("header")))
                    succeeded = run_interaction_group(
                        ctx, driver, broker, course_url, course_title,
                        work_item, model)
                done += int(bool(succeeded))
                skipped += int(not succeeded)
            except (LLMError, RuntimeError, WebDriverException) as exc:
                skipped += 1
                label = (work_item if isinstance(work_item, str)
                         else work_item.get("header"))
                ctx.log(f"任务失败: {label}: {exc}")
            ctx.progress(done=done, total=len(work), course=course_title)
        ctx.status(
            f"答题与互动处理结束: 完成 {done}/{len(work)}，跳过或失败 {skipped}",
            done=True)
    finally:
        broker.clear()


def build_combined_work(
        cache: dict, courses: list[tuple[str, str]], plans: dict,
        auto_answer: bool, comment_reply: bool) -> tuple[
            list[dict], list[dict], list[dict]]:
    if not isinstance(plans, dict):
        plans = {}
    video_jobs: dict[tuple[str, str], dict] = {}
    quiz_items: list[dict] = []
    interaction_items: list[dict] = []
    for course_url, cid in courses:
        course = cache["courses"].get(cid)
        if not isinstance(course, dict):
            continue
        plan = plans.get(cid)
        if not isinstance(plan, dict):
            plan = {"enabled": True, "scope": "all", "task_ids": []}
        if not plan.get("enabled", True):
            continue
        tasks = [
            task for task in course.get("tasks", [])
            if isinstance(task, dict)
        ]
        scope = str(plan.get("scope") or "all")
        if scope == "selected":
            selected_ids = {
                str(task_id) for task_id in plan.get("task_ids", [])
            }
            tasks = [
                task for task in tasks if task.get("id") in selected_ids
            ]
        course_title = normalize_title(course.get("title")) or cid
        for task in tasks:
            for video in task.get("videos", []):
                title = normalize_title(video.get("title"))
                video_jobs[(cid, title)] = {
                    "title": title,
                    "course_url": course_url,
                    "cid": cid,
                    "course_title": course_title,
                }
            if auto_answer:
                for test in task.get("tests", []):
                    if "已提交" in test.get("tags", []):
                        continue
                    quiz_items.append({
                        "course_url": course_url,
                        "course_title": course_title,
                        "title": normalize_title(test.get("title")),
                    })
            if comment_reply and (
                    task_group_has(task, "评论")
                    or task_group_has(task, "回复")):
                interaction_items.append({
                    "course_url": course_url,
                    "course_title": course_title,
                    "group": task,
                })
        if scope == "all_extra":
            for video in course.get("extra_videos", []):
                title = normalize_title(video.get("title"))
                video_jobs[(cid, title)] = {
                    "title": title,
                    "course_url": course_url,
                    "cid": cid,
                    "course_title": course_title,
                }
    return list(video_jobs.values()), quiz_items, interaction_items


def run_combined(ctx: PluginContext, driver,
                 courses: list[tuple[str, str]]) -> None:
    cache = load_task_cache(ctx)
    if not cache.get("courses"):
        raise RuntimeError(
            "尚未生成课程任务缓存，请先在插件配置中点击“刷新课程任务缓存”")
    auto_answer = bool(ctx.cfg("auto_answer", False))
    comment_reply = bool(ctx.cfg("comment_reply", False))
    full_auto = bool(ctx.cfg("full_auto", False)) and (
        auto_answer or comment_reply)
    jobs, quiz_items, interaction_items = build_combined_work(
        cache,
        courses,
        ctx.cfg("course_plans", {}),
        auto_answer,
        comment_reply,
    )
    total_work = len(jobs) + len(quiz_items) + len(interaction_items)
    if not total_work:
        ctx.progress(done=0, total=0)
        ctx.status("所选范围内没有需要处理的内容", done=True)
        return
    ctx.log(
        f"合并任务开始: 视频 {len(jobs)}, 测验 {len(quiz_items)}, "
        f"评论回复 {len(interaction_items)}, 总计 {total_work}")
    if full_auto:
        ctx.log("警告: 全自动模式已启用，答题和互动内容将不经人工核验直接提交")

    done = 0
    skipped = 0
    if jobs:
        workers = min(max(1, positive_int(ctx.cfg("workers", 2), 2)), len(jobs))
        ctx.log(f"开始播放任务范围内视频: {len(jobs)} 个, {workers} 并发")
        tracker = run_parallel(
            ctx,
            jobs,
            driver.get_cookies(),
            None,
            bounded_float(ctx.cfg("speedup", 3), 3.0, 1.0, 3.0),
            bounded_float(
                ctx.cfg("completion_tolerance", 3), 3.0, 0.0, 30.0),
            bool(ctx.cfg("only_unfinished", True)),
            workers,
        )
        done = sum(
            1 for status in tracker.results.values() if status == "已完成")
        skipped += len(jobs) - done

    model = None
    broker = None
    if quiz_items or interaction_items:
        api_url = str(
            ctx.cfg("api_url", "https://api.deepseek.com")
            or "https://api.deepseek.com").strip()
        api_key = str(ctx.cfg("api_key") or "").strip()
        model_id = str(
            ctx.cfg("model_id", "deepseek-flash")
            or "deepseek-flash").strip()
        if not api_key:
            raise RuntimeError("答题或评论回复已启用，但未配置模型 API Key")
        model = (api_url, api_key, model_id)
        broker = ReviewBroker(ctx)
        broker.clear()

    try:
        for item in quiz_items:
            ctx.progress(
                done=done, total=total_work, course=item["course_title"],
                title=item["title"])
            driver.get(item["course_url"])
            try:
                if run_quiz_item(
                        ctx, driver, broker, item["course_title"],
                        item["title"], model, full_auto=full_auto):
                    done += 1
                else:
                    skipped += 1
            except (LLMError, RuntimeError, WebDriverException) as exc:
                skipped += 1
                ctx.log(f"测验失败: {item['title']}: {exc}")
        for item in interaction_items:
            group = item["group"]
            ctx.progress(
                done=done, total=total_work, course=item["course_title"],
                title=normalize_title(group.get("description")))
            try:
                if run_interaction_group(
                        ctx, driver, broker, item["course_url"],
                        item["course_title"], group, model,
                        full_auto=full_auto):
                    done += 1
                else:
                    skipped += 1
            except (LLMError, RuntimeError, WebDriverException) as exc:
                skipped += 1
                ctx.log(
                    f"评论回复失败: {group.get('description', '')}: {exc}")
    finally:
        if broker:
            broker.clear()
    ctx.progress(done=done, total=total_work)
    ctx.status(
        f"合并任务结束: 完成 {done}/{total_work}，跳过或失败 {skipped}",
        done=True)


def main() -> None:
    ctx = PluginContext()
    raw_urls = ctx.cfg("course_urls") or []
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls]
    if not isinstance(raw_urls, (list, tuple)):
        raw_urls = []

    courses: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in raw_urls:
        parsed = parse_course_url(raw)
        if parsed and parsed[1] not in seen:
            seen.add(parsed[1])
            courses.append(parsed)
    if not courses:
        ctx.log("未配置有效的师道云课程链接(/course?cid=...),退出")
        return
    if not ctx.username or not ctx.password:
        ctx.log("未配置统一认证账号(需在全局设置或插件配置中填写),退出")
        return

    task_mode = str(
        ctx.cfg("task_mode", "combined") or "combined").strip().lower()
    if task_mode not in {"crawl", "combined"}:
        task_mode = "combined"
    auto_enabled = bool(
        ctx.cfg("auto_answer", False) or ctx.cfg("comment_reply", False))
    if task_mode == "combined" and auto_enabled and not str(
            ctx.cfg("api_key") or "").strip():
        ctx.log("答题或评论回复已启用，但未配置模型 API Key，退出")
        return
    headless = bool(ctx.cfg("headless", True))

    driver = make_driver(ctx, headless=headless)
    driver.set_page_load_timeout(60)
    try:
        first_url, first_cid = courses[0]
        ensure_authenticated(ctx, driver, first_url, first_cid)
        if task_mode == "crawl":
            ctx.log("开始刷新课程任务缓存")
            refresh_task_cache(ctx, driver, courses)
            ctx.status("课程任务缓存已更新", done=True)
            return
        run_combined(ctx, driver, courses)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
