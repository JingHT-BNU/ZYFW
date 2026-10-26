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
import spoc as spoc_platform  # noqa: E402

LOGIN_HOME = "https://bdata.bnu.edu.cn/"
COURSE_DATA_API = "/wechatapi/api/learn/getCourseData"
COURSE_LIST_API = "/wechatapi/api/learn/getMyCourses"
USER_BINDS_API = "/wechatapi/api/user/getMyBinds"
COOKIE_LIFE_SECONDS = 7 * 86400
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
    existing: list[dict] = []
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if normalize_title(old.get("account")) == normalize_title(ctx.username):
                existing = old.get("cookies") or []
        except (OSError, ValueError, TypeError):
            existing = []
    merged: dict[tuple[str, str, str], dict] = {}
    for cookie in [*existing, *cookies]:
        if not isinstance(cookie, dict):
            continue
        key = (
            str(cookie.get("name") or ""),
            str(cookie.get("domain") or ""),
            str(cookie.get("path") or "/"),
        )
        merged[key] = cookie
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=".bdata-cookies-", suffix=".tmp", delete=False,
        ) as f:
            tmp_path = f.name
            json.dump({
                "saved_at": time.time(),
                "account": normalize_title(ctx.username),
                "cookies": list(merged.values()),
            },
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
        if normalize_title(data.get("account")) != normalize_title(ctx.username):
            return []
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


def inject_domain_cookies(driver, url: str, cookies: list[dict]) -> None:
    driver.get(url)
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


def _course_items(value: object) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("list", "items", "courses", "data"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def list_my_courses(driver) -> list[dict]:
    """Return every assigned BData course visible to the logged-in account."""
    body = api_json(
        driver,
        COURSE_LIST_API,
        {"currentPage": 1, "pageSize": 10000},
    )
    courses: list[dict] = []
    seen: set[str] = set()
    for item in _course_items(body.get("data")):
        cid = normalize_title(
            item.get("_id") or item.get("cid") or item.get("id"))
        if not cid or cid in seen:
            continue
        seen.add(cid)
        courses.append({
            "cid": cid,
            "url": f"https://bdata.bnu.edu.cn/course?cid={cid}",
            "title": normalize_title(
                item.get("title") or item.get("name")
                or item.get("courseName") or item.get("cname")),
        })
    return courses


def _bound_bnu_account(driver) -> str:
    body = api_json(driver, USER_BINDS_API, {})
    binds = body.get("data")
    if isinstance(binds, dict):
        binds = binds.get("binds") or binds.get("list") or []
    if not isinstance(binds, list):
        return ""
    for bind in binds:
        value = normalize_title(bind)
        if value.lower().startswith("bnucas_"):
            return value.split("_", 1)[1].strip()
    return ""


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
    const xpath = el => {
      const parts = [];
      let node = el;
      while (node && node.nodeType === 1) {
        if (node.id) {
          parts.unshift(`//*[@id="${node.id}"]`);
          return parts.join('/');
        }
        let index = 1;
        let sibling = node;
        while ((sibling = sibling.previousElementSibling)) {
          if (sibling.tagName === node.tagName) index += 1;
        }
        parts.unshift(`${node.tagName.toLowerCase()}[${index}]`);
        node = node.parentElement;
      }
      return '/' + parts.join('/');
    };
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
                    xpath: xpath(span)
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


def click_task_entry_xpath(driver, xpath: str) -> bool:
    if not xpath:
        return False
    try:
        element = driver.find_element(By.XPATH, xpath)
    except WebDriverException:
        return False
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});"
        "arguments[0].click();", element)
    deadline = time.time() + 15
    while time.time() < deadline:
        if not _task_pane_ready(driver, require_cards=False):
            return True
        time.sleep(0.2)
    return False


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


def task_cache_account(ctx: PluginContext) -> str:
    return normalize_title(ctx.username)


def _read_task_cache_store(ctx: PluginContext) -> dict:
    data = _read_json_object(task_cache_path(ctx))
    if not data or isinstance(data.get("version"), int):
        return {}
    return data


def load_task_cache(ctx: PluginContext) -> dict:
    account = task_cache_account(ctx)
    store = _read_task_cache_store(ctx)
    entry = store.get(account)
    if not isinstance(entry, dict):
        entry = {}
    return {
        "account": account,
        "updated_at": str(entry.get("updated_at") or ""),
        "bdata": entry.get("bdata")
        if isinstance(entry.get("bdata"), dict) else {},
        "spoc": entry.get("spoc")
        if isinstance(entry.get("spoc"), list) else [],
    }


def save_task_cache(ctx: PluginContext, data: dict) -> None:
    account = task_cache_account(ctx)
    if not account:
        raise RuntimeError("无法保存课程缓存: 统一认证账号为空")
    store = _read_task_cache_store(ctx)
    updated_at = str(
        data.get("updated_at") or time.strftime("%Y-%m-%d %H:%M:%S"))
    store[account] = {
        "updated_at": updated_at,
        "bdata": data.get("bdata")
        if isinstance(data.get("bdata"), dict) else {},
        "spoc": data.get("spoc")
        if isinstance(data.get("spoc"), list) else [],
    }
    _write_json_atomic(task_cache_path(ctx), store)


def collect_course_menu_structure(driver) -> tuple[list[dict], list[dict]]:
    """Return BData top-level chapters and their video entries."""
    deadline = time.time() + 8
    chapters: list[dict] = []
    while time.time() < deadline:
        rows = driver.execute_script(
            """
            const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
            const xpath = el => {
              const parts = [];
              let node = el;
              while (node && node.nodeType === 1) {
                if (node.id) {
                  parts.unshift(`//*[@id="${node.id}"]`);
                  return parts.join('/');
                }
                let index = 1;
                let sibling = node;
                while ((sibling = sibling.previousElementSibling)) {
                  if (sibling.tagName === node.tagName) index += 1;
                }
                parts.unshift(
                  `${node.tagName.toLowerCase()}[${index}]`);
                node = node.parentElement;
              }
              return '/' + parts.join('/');
            };
            return Array.from(document.querySelectorAll(
                '.el-menu > li.el-sub-menu')).map((chapter, index) => {
              const title = chapter.querySelector(
                  ':scope > .el-sub-menu__title');
              const items = Array.from(chapter.querySelectorAll(
                  ':scope > ul li.el-menu-item')).map((item, videoIndex) => {
                const path = item.querySelector('svg path');
                return {
                  index: videoIndex,
                  title: norm(item.innerText),
                  icon: path ? (path.getAttribute('d') || '') : '',
                  xpath: xpath(item)
                };
              }).filter(item => item.title &&
                  item.icon.startsWith(arguments[0]));
              return {
                index,
                name: norm(title && title.innerText),
                xpath: xpath(chapter),
                videos: items
              };
            }).filter(chapter => chapter.name && chapter.videos.length);
            """,
            TASK_VIDEO_ICON_PREFIX,
        )
        chapters = rows if isinstance(rows, list) else []
        if chapters:
            break
        time.sleep(0.3)

    result: list[dict] = []
    videos: dict[str, dict] = {}
    for chapter_index, chapter in enumerate(chapters):
        name = normalize_title(chapter.get("name"))
        chapter_videos = []
        for video_index, video in enumerate(chapter.get("videos", [])):
            title = normalize_title(video.get("title"))
            if not title:
                continue
            entry = {
                "index": video_index,
                "title": title,
                "xpath": str(video.get("xpath") or ""),
            }
            chapter_videos.append(entry)
            videos[title] = entry
        if chapter_videos:
            result.append({
                "index": chapter_index,
                "name": name,
                "xpath": str(chapter.get("xpath") or ""),
                "videos": chapter_videos,
            })
    return result, list(videos.values())


def collect_course_menu_videos(driver) -> list[dict]:
    return collect_course_menu_structure(driver)[1]


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
            "xpath": str(item.get("xpath") or ""),
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
        "id": f"bdata:{cid}:task:{number}",
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


def _tag_completion(tags: list[str]) -> tuple[str, bool]:
    progress = ""
    finished = False
    for tag in tags:
        value = normalize_title(tag)
        if "已完成" in value or "已提交" in value:
            finished = True
        if "%" in value and not progress:
            progress = value
    return progress, finished


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
    chapter_rows, _menu_videos = collect_course_menu_structure(driver)
    task_video_by_title = {
        normalize_title(video.get("title")): (task_index, video)
        for task_index, task in enumerate(tasks)
        for video in task.get("videos", [])
    }
    task_exam_by_title = {
        normalize_title(exam.get("title")): (task_index, exam)
        for task_index, task in enumerate(tasks)
        for exam in task.get("tests", [])
    }
    chapters: dict[str, dict] = {}
    video_lookup: dict[str, tuple[str, str]] = {}
    exam_lookup: dict[str, tuple[str, str]] = {}
    for chapter_index, row in enumerate(chapter_rows):
        chapter_key = str(chapter_index)
        chapter = {
            "name": row["name"],
            "XPATH": row["xpath"],
            "videos": {},
            "exams": {},
        }
        for video_index, video in enumerate(row.get("videos", [])):
            name = normalize_title(video.get("title"))
            task_index, task_video = task_video_by_title.get(
                name, (-1, {}))
            progress, finished = _tag_completion(
                task_video.get("tags", []))
            task = tasks[task_index] if task_index >= 0 else {}
            completion = task.get("completion", {})
            comments = completion.get("comments") or {}
            replies = completion.get("replies") or {}
            video_key = str(video_index)
            chapter["videos"][video_key] = {
                "name": name,
                "XPATH": video.get("xpath", ""),
                "progress": progress,
                "total": "",
                "finished": finished,
                "commit": int(
                    comments.get("done", 0) >= comments.get("total", 1)
                ) if comments else 0,
                "reply": int(
                    replies.get("done", 0) >= replies.get("total", 1)
                ) if replies else 0,
            }
            video_lookup[name] = (chapter_key, video_key)
        chapters[chapter_key] = chapter

    # Tests are present in the task dialog rather than the visible chapter menu.
    # Attach them to the chapter containing the task's first required video.
    for exam_title, (task_index, exam) in task_exam_by_title.items():
        task = tasks[task_index]
        chapter_key = ""
        for video in task.get("videos", []):
            location = video_lookup.get(normalize_title(video.get("title")))
            if location:
                chapter_key = location[0]
                break
        if not chapter_key:
            continue
        chapter = chapters[chapter_key]
        exam_key = str(len(chapter["exams"]))
        _progress, finished = _tag_completion(exam.get("tags", []))
        chapter["exams"][exam_key] = {
            "name": exam_title,
            "XPATH": exam.get("xpath", ""),
            "finished": finished,
        }
        exam_lookup[exam_title] = (chapter_key, exam_key)

    compact_tasks: list[dict] = []
    for task in tasks:
        video_refs: dict[str, list[str]] = {}
        for video in task.get("videos", []):
            location = video_lookup.get(normalize_title(video.get("title")))
            if location:
                video_refs.setdefault(location[0], []).append(location[1])
        exam_refs: dict[str, list[str]] = {}
        for exam in task.get("tests", []):
            location = exam_lookup.get(normalize_title(exam.get("title")))
            if location:
                exam_refs.setdefault(location[0], []).append(location[1])
        completion = task.get("completion", {})
        comments = completion.get("comments") or {}
        replies = completion.get("replies") or {}
        questionnaire = completion.get("questionnaires") or {}
        compact_tasks.append({
            "name": task["description"],
            "videos": video_refs,
            "exams": exam_refs,
            "total_video_progress": completion.get("video_percent"),
            "commit_progress": bool(
                comments and comments.get("done", 0) >= comments.get(
                    "total", 1)),
            "reply_progress": bool(
                replies and replies.get("done", 0) >= replies.get(
                    "total", 1)),
            "questionnaire_progress": bool(
                questionnaire
                and questionnaire.get("done", 0) >= questionnaire.get(
                    "total", 1)),
            "exam_progress": sum(
                1 for exam in task.get("tests", [])
                if _tag_completion(exam.get("tags", []))[1]),
            "finished": bool(completion.get("complete")),
        })
    return {
        "name": title,
        "chapters": chapters,
        "tasks": compact_tasks,
    }


def refresh_task_cache(ctx: PluginContext, driver,
                       discovered: list[dict]) -> dict:
    cache = load_task_cache(ctx)
    errors = 0
    current_ids = {str(course["cid"]) for course in discovered}
    for index, course_info in enumerate(discovered, start=1):
        course_url = course_info["url"]
        cid = course_info["cid"]
        title = course_info.get("title") or cid
        ctx.progress(
            stage="cache", done=index - 1, total=len(discovered),
            title=f"读取任务缓存: {title}")
        try:
            course = cache_course_tasks(ctx, driver, course_url, cid)
            cache["bdata"][cid] = course
            cache["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_task_cache(ctx, cache)
            ctx.log(
                f"已缓存课程 {course['name']}: "
                f"{len(course['tasks'])} 个任务, "
                f"{len(course['chapters'])} 个章节")
        except (RuntimeError, WebDriverException) as exc:
            errors += 1
            ctx.log(f"课程任务缓存失败({cid}): {exc}")
    ctx.progress(
        stage="cache", done=len(discovered), total=len(discovered),
        title="任务缓存完成")
    if errors == 0:
        cache["bdata"] = {
            cid: course for cid, course in cache["bdata"].items()
            if cid in current_ids
        }
    cache["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_task_cache(ctx, cache)
    if errors == len(discovered):
        raise RuntimeError("全部课程任务缓存失败")
    return cache


def refresh_spoc_cache(ctx: PluginContext, driver) -> tuple[dict, int, int]:
    cache = load_task_cache(ctx)
    discovered_urls: set[str] = set()

    def report(done: int, total: int, title: str) -> None:
        ctx.progress(
            stage="cache", done=done, total=total,
            title=f"读取 SPOC 课程: {title}")

    def save_course(course: dict) -> None:
        url = str(course.get("url") or "")
        discovered_urls.add(url)
        cache["spoc"] = [
            item for item in cache["spoc"]
            if item.get("url") != url
        ]
        cache["spoc"].append(course)
        cache["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_task_cache(ctx, cache)

    try:
        failed = spoc_platform.cache_all_courses(
            ctx, driver, report, save_course)
    except (RuntimeError, WebDriverException) as exc:
        ctx.log(f"SPOC 全量课程爬取失败: {exc}")
        return cache, 0, 1
    if discovered_urls and failed == 0:
        cache["spoc"] = [
            course for course in cache["spoc"]
            if course.get("url") in discovered_urls
        ]
        cache["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_task_cache(ctx, cache)
    return cache, len(discovered_urls), failed


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


def _login(ctx: PluginContext, driver) -> None:
    driver.get(LOGIN_HOME)
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


def ensure_authenticated(ctx: PluginContext, driver,
                         discover_courses: bool = True) -> list[dict]:
    expected_account = task_cache_account(ctx)
    if not expected_account:
        raise RuntimeError("未配置统一身份认证账号")
    cookies = load_cookies(ctx)
    if cookies:
        inject_cookies(driver, cookies)
        driver.get(LOGIN_HOME)
        time.sleep(1.5)
        try:
            bound_account = _bound_bnu_account(driver)
            if bound_account == expected_account:
                courses = list_my_courses(driver) if discover_courses else []
                ctx.log(
                    "使用缓存 Cookie 登录师道云"
                    + (f"，发现 {len(courses)} 门课程"
                       if discover_courses else ""))
                return courses
            if bound_account:
                ctx.log(
                    f"缓存 Cookie 属于账号 {bound_account}，"
                    f"当前配置为 {expected_account}，重新登录")
            else:
                ctx.log("缓存 Cookie 未绑定当前统一认证账号，重新登录")
        except RuntimeError:
            ctx.log("缓存 Cookie 已失效, 重新执行统一身份认证")

        try:
            driver.delete_all_cookies()
        except WebDriverException:
            pass
        cookie_file(ctx).unlink(missing_ok=True)

    ctx.status("正在登录北师大统一身份认证…")
    _login(ctx, driver)
    driver.get(LOGIN_HOME)
    bound_account = _bound_bnu_account(driver)
    if bound_account != expected_account:
        actual = bound_account or "未识别"
        raise RuntimeError(
            f"登录账号校验失败: 配置为 {expected_account}, 网页账号为 {actual}")
    courses = list_my_courses(driver) if discover_courses else []
    save_cookies(ctx, driver.get_cookies())
    ctx.log("统一身份认证登录成功"
            + (f"，发现 {len(courses)} 门课程" if discover_courses else "")
            + "，Cookie 已缓存")
    return courses


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


def click_menu_xpath(driver, xpath: str) -> bool:
    if not xpath:
        return False
    try:
        element = driver.find_element(By.XPATH, xpath)
    except WebDriverException:
        return False
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});"
        "arguments[0].click();", element)
    return True


def wait_and_click_menu_title(driver, title: str,
                              timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if click_menu_title(driver, title):
            return True
        time.sleep(0.4)
    return False


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


def _course_plan(plans: dict, key: str) -> dict:
    plan = plans.get(key)
    if not isinstance(plan, dict):
        plan = plans.get(key.replace("bdata:", "", 1))
    return plan if isinstance(plan, dict) else {
        "enabled": True,
        "scope": "all",
        "task_ids": [],
        "chapter_ids": [],
    }


def _bdata_job(course: dict, video: dict) -> dict:
    return {
        "platform": "bdata",
        "key": f"bdata:{course['cid']}:{video.get('XPATH') or video['name']}",
        "title": normalize_title(video.get("name")),
        "course_id": f"bdata:{course['cid']}",
        "course_title": course.get("name") or course["cid"],
        "course_url": (
            "https://bdata.bnu.edu.cn/course?cid=" + course["cid"]),
        "cid": course["cid"],
        "xpath": str(video.get("XPATH") or ""),
    }


def _spoc_job(course: dict, video: dict) -> dict:
    parsed = spoc_platform.parse_video_url(video.get("url"))
    if not parsed:
        return {}
    sign, cid, leaf_id = parsed
    return {
        "platform": "spoc",
        "key": f"spoc:{sign}:{cid}:{leaf_id}",
        "title": normalize_title(video.get("name")),
        "course_id": course["url"],
        "course_title": course.get("name") or cid,
        "course_url": course["url"],
        "sign": sign,
        "cid": cid,
        "leaf_id": leaf_id,
    }


def build_unified_work(
        cache: dict, plans: dict, auto_answer: bool,
        comment_reply: bool) -> tuple[list[dict], list[dict], list[dict]]:
    bdata_jobs: dict[str, dict] = {}
    spoc_jobs: dict[str, dict] = {}
    quiz_items: list[dict] = []
    interaction_items: list[dict] = []
    plans = plans if isinstance(plans, dict) else {}
    for cid, raw_course in cache.get("bdata", {}).items():
        if not isinstance(raw_course, dict):
            continue
        course = {**raw_course, "cid": str(cid)}
        plan = _course_plan(plans, f"bdata:{cid}")
        if plan.get("enabled") is False:
            continue
        scope = str(plan.get("scope") or "all")
        course_title = normalize_title(course.get("name")) or str(cid)
        chapter_data = course.get("chapters")
        chapters = chapter_data if isinstance(chapter_data, dict) else {}
        tasks = [
            task for task in course.get("tasks", [])
            if isinstance(task, dict)
        ]
        chapter_ids = {
            str(value).rsplit(":chapter:", 1)[-1]
            for value in plan.get("chapter_ids", [])
        }
        task_ids = {str(value) for value in plan.get("task_ids", [])}
        selected_tasks = [
            task for index, task in enumerate(tasks)
            if str(index) in task_ids
        ]
        if scope in {"all", "all_extra"}:
            selected_tasks = tasks
        elif scope == "chapters":
            selected_tasks = [
                task for task in tasks
                if chapter_ids.intersection(
                    str(chapter_key)
                    for chapter_key in task.get("videos", {}))
            ]

        videos: list[dict] = []
        if scope in {"all_extra", "chapters"}:
            for chapter_key, chapter in chapters.items():
                if scope == "chapters" and str(chapter_key) not in chapter_ids:
                    continue
                for video in chapter.get("videos", {}).values():
                    if isinstance(video, dict):
                        videos.append(video)
        else:
            for task in selected_tasks:
                for chapter_key, indexes in task.get("videos", {}).items():
                    chapter_videos = chapters.get(
                        str(chapter_key), {}).get("videos", {})
                    videos.extend(
                        chapter_videos[str(index)]
                        for index in indexes
                        if str(index) in chapter_videos)
        for video in videos:
            job = _bdata_job(course, video)
            bdata_jobs[job["key"]] = job

        course_url = "https://bdata.bnu.edu.cn/course?cid=" + str(cid)
        if auto_answer:
            seen_exams: set[tuple[str, str]] = set()
            for task in selected_tasks:
                for chapter_key, indexes in task.get("exams", {}).items():
                    chapter_exams = chapters.get(
                        str(chapter_key), {}).get("exams", {})
                    for index in indexes:
                        exam = chapter_exams.get(str(index))
                        if not isinstance(exam, dict) or exam.get("finished"):
                            continue
                        key = (str(chapter_key), str(index))
                        if key in seen_exams:
                            continue
                        seen_exams.add(key)
                        quiz_items.append({
                            "course_url": course_url,
                            "course_title": course_title,
                            "title": normalize_title(exam.get("name")),
                            "xpath": str(exam.get("XPATH") or ""),
                        })
        if comment_reply:
            for task in selected_tasks:
                if (task.get("commit_progress") is not False
                        and task.get("reply_progress") is not False):
                    continue
                task_videos = []
                for chapter_key, indexes in task.get("videos", {}).items():
                    chapter_videos = chapters.get(
                        str(chapter_key), {}).get("videos", {})
                    for index in indexes:
                        video = chapter_videos.get(str(index))
                        if isinstance(video, dict):
                            task_videos.append(video)
                if not task_videos:
                    continue
                metrics = []
                if task.get("commit_progress") is False:
                    metrics.append("评论0/1")
                if task.get("reply_progress") is False:
                    metrics.append("回复0/1")
                interaction_items.append({
                    "course_url": course_url,
                    "course_title": course_title,
                    "group": {
                        "description": task.get("name", ""),
                        "metrics": metrics,
                        "items": [{
                            "title": video["name"],
                            "icon": TASK_VIDEO_ICON_PREFIX,
                        } for video in task_videos],
                        "video_xpath": task_videos[0].get("XPATH", ""),
                    },
                })

    for course in cache.get("spoc", []):
        if not isinstance(course, dict) or not course.get("url"):
            continue
        plan = _course_plan(plans, course["url"])
        if plan.get("enabled") is False:
            continue
        scope = str(plan.get("scope") or "all")
        chapters = course.get("chapters")
        chapters = chapters if isinstance(chapters, dict) else {}
        chapter_ids = {
            str(value).rsplit(":chapter:", 1)[-1]
            for value in plan.get("chapter_ids", [])
        }
        for chapter_key, chapter in chapters.items():
            if scope == "chapters" and str(chapter_key) not in chapter_ids:
                continue
            for video in chapter.get("videos", {}).values():
                if not isinstance(video, dict):
                    continue
                job = _spoc_job(course, video)
                if job:
                    spoc_jobs[job["key"]] = job

    jobs: list[dict] = []
    bdata_values = list(bdata_jobs.values())
    spoc_values = list(spoc_jobs.values())
    for index in range(max(len(bdata_values), len(spoc_values))):
        if index < len(spoc_values):
            jobs.append(spoc_values[index])
        if index < len(bdata_values):
            jobs.append(bdata_values[index])
    return jobs, quiz_items, interaction_items


def _play_bdata_job(ctx: PluginContext, driver, job: dict, worker_id: int,
                    tracker: PlayTracker, state: dict, speedup: float,
                    tolerance: float, only_unfinished: bool) -> None:
    title = job["title"]
    tracker.set_worker(
        worker_id, platform="师道云", status="检查中", title=title,
        percent=0.0, eta_s=None, total_s=None,
        course=job["course_title"])
    if state.get("bdata_course") != job["course_url"]:
        driver.get(job["course_url"])
        state["bdata_course"] = job["course_url"]
        time.sleep(0.8)
    if not click_menu_xpath(driver, job.get("xpath", "")):
        if not wait_and_click_menu_title(driver, title):
            ensure_authenticated(ctx, driver)
            driver.get(job["course_url"])
            state["bdata_course"] = job["course_url"]
            if not click_menu_xpath(driver, job.get("xpath", "")) \
                    and not wait_and_click_menu_title(driver, title):
                raise RuntimeError("师道云课程目录中未找到该视频")
    wait_active_menu_title(driver, title)
    media_duration = wait_video_duration(driver)
    if media_duration <= 0:
        raise RuntimeError("师道云视频时长不可用")
    page_label = wait_duration_label(driver)
    watched = float(parse_duration_seconds(page_label))
    if only_unfinished and (
            "已完成" in page_label
            or is_complete(watched, media_duration, tolerance)):
        tracker.mark(job["key"], "已完成")
        tracker.set_worker(
            worker_id, status="已完成", title=title, percent=100.0,
            eta_s=0, total_s=round(media_duration))
        ctx.log(
            f"[w{worker_id}] 师道云跳过已完成 "
            f"{watched:.0f}/{media_duration:.0f}s: {title}")
        return

    def report(fields: dict) -> None:
        tracker.set_worker(
            worker_id,
            status="重播中" if fields.get("replaying") else "播放中",
            title=fields["title"], percent=fields["percent"],
            eta_s=fields["eta_s"],
            total_s=round(fields["target_seconds"]),
            course=job["course_title"],
            replays=fields.get("replays", 0))

    ok, watched, media_duration = play_until_complete(
        driver, title, media_duration, watched, speedup, tolerance,
        on_progress=report)
    if not ok:
        raise RuntimeError(
            f"师道云服务器进度未达到视频时长"
            f"({watched:.0f}/{media_duration:.0f}s)")
    tracker.mark(job["key"], "已完成")
    tracker.set_worker(
        worker_id, status="已完成", title=title, percent=100.0,
        eta_s=0, total_s=round(media_duration))
    ctx.log(
        f"[w{worker_id}] 师道云完成 "
        f"{watched:.0f}/{media_duration:.0f}s: {title}")


def _play_spoc_job(ctx: PluginContext, driver, job: dict, worker_id: int,
                   tracker: PlayTracker, state: dict, speedup: float,
                   only_unfinished: bool) -> None:
    title = job["title"]
    tracker.set_worker(
        worker_id, platform="SPOC", status="检查中", title=title,
        percent=0.0, eta_s=None, total_s=None,
        course=job["course_title"])

    def report(percent: float, remain_s: float | None,
               duration: float) -> None:
        tracker.set_worker(
            worker_id, status="播放中", title=title,
            percent=round(percent, 1),
            eta_s=round(remain_s) if remain_s is not None else None,
            total_s=round(duration) if duration else None,
            course=job["course_title"])

    status = spoc_platform.play_video(
        driver, job["sign"], job["cid"], job["leaf_id"], speedup,
        only_unfinished=only_unfinished, on_progress=report)
    if status in {"页面无播放器", "找不到播放按钮"}:
        tracker.set_worker(
            worker_id, status="重登中", title=title,
            percent=None, eta_s=None, total_s=None)
        state["cookies"] = spoc_platform.ensure_login(
            ctx, driver, state.get("cookies"))
        status = spoc_platform.play_video(
            driver, job["sign"], job["cid"], job["leaf_id"], speedup,
            only_unfinished=only_unfinished, on_progress=report)
    complete = status in {"已完成", "服务器已记录"}
    tracker.mark(job["key"], "已完成" if complete else status)
    tracker.set_worker(
        worker_id, status="已完成" if complete else status, title=title,
        percent=100.0 if complete else None, eta_s=0 if complete else None,
        total_s=None)
    ctx.log(f"[w{worker_id}] SPOC {title} → {status}")


def run_worker(ctx: PluginContext, worker_id: int, jobs: queue.Queue,
               cookies: list[dict], tracker: PlayTracker,
               speedup: float, tolerance: float, only_unfinished: bool,
               initial_driver=None) -> None:
    driver = initial_driver
    state = {"bdata_course": "", "cookies": list(cookies)}
    try:
        if driver is None:
            driver = make_driver(ctx, headless=bool(ctx.cfg("headless", True)))
        inject_domain_cookies(driver, LOGIN_HOME, cookies)
        inject_domain_cookies(driver, spoc_platform.LOGIN_HOME, cookies)

        while True:
            try:
                job = jobs.get_nowait()
            except queue.Empty:
                tracker.set_worker(
                    worker_id, status="空闲", eta_s=None)
                return
            title = job["title"]
            try:
                if job["platform"] == "spoc":
                    _play_spoc_job(
                        ctx, driver, job, worker_id, tracker, state,
                        speedup, only_unfinished)
                else:
                    _play_bdata_job(
                        ctx, driver, job, worker_id, tracker, state,
                        speedup, tolerance, only_unfinished)
            except Exception as e:
                tracker.mark(job["key"], "出错")
                tracker.set_worker(
                    worker_id, status="出错", title=title, percent=None,
                    eta_s=None, total_s=None, platform=(
                        "SPOC" if job["platform"] == "spoc" else "师道云"))
                ctx.log(
                    f"[w{worker_id}] {job['platform']} 视频失败: "
                    f"{title}: {e!r}")
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
    threads = []
    for worker_id in range(min(worker_count, len(jobs))):
        thread = threading.Thread(
            target=run_worker,
            args=(
                ctx, worker_id, work, cookies, tracker,
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
                  full_auto: bool = False,
                  entry_xpath: str = "") -> bool:
    open_task_dialog(driver)
    if not click_task_entry_xpath(driver, entry_xpath):
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
    if not click_task_entry_xpath(
            driver, str(group.get("video_xpath") or "")):
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
            f"答题与互动处理结束: 完成 {done}/{len(work)}，跳过或失败 {skipped}，如有失败可尝试重启重试",
            done=True)
    finally:
        broker.clear()


def run_qa_work(ctx: PluginContext, driver,
                work: list[tuple[tuple[str, str], str, dict | str, str]],
                full_auto: bool = False) -> tuple[int, int]:
    """Run cached BData quiz/interaction work without touching video progress."""
    if not work:
        return 0, 0
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
    done = 0
    skipped = 0
    ctx.log(f"答题与互动任务共 {len(work)} 项，开始并行处理")
    try:
        for course, course_title, work_item, entry_xpath in work:
            course_url, _cid = course
            driver.get(course_url)
            try:
                if isinstance(work_item, str):
                    ctx.log(
                        f"答题进度 {done + skipped + 1}/{len(work)}: "
                        f"{course_title} · {work_item}")
                    succeeded = run_quiz_item(
                        ctx, driver, broker, course_title, work_item, model,
                        full_auto=full_auto,
                        entry_xpath=entry_xpath)
                else:
                    label = normalize_title(work_item.get("description"))
                    ctx.log(
                        f"互动进度 {done + skipped + 1}/{len(work)}: "
                        f"{course_title} · {label}")
                    succeeded = run_interaction_group(
                        ctx, driver, broker, course_url, course_title,
                        work_item, model, full_auto=full_auto)
                done += int(bool(succeeded))
                skipped += int(not succeeded)
            except (LLMError, RuntimeError, WebDriverException) as exc:
                skipped += 1
                label = (work_item if isinstance(work_item, str)
                         else work_item.get("description"))
                ctx.log(f"答题或互动失败: {label}: {exc}")
    finally:
        broker.clear()
    ctx.log(
        f"答题与互动处理结束: 完成 {done}/{len(work)}，"
        f"跳过或失败 {skipped}")
    return done, skipped


def run_qa_worker(ctx: PluginContext,
                  work: list[tuple[tuple[str, str], str, dict | str, str]],
                  cookies: list[dict], full_auto: bool,
                  result: dict) -> None:
    driver = None
    try:
        driver = make_driver(ctx, headless=bool(ctx.cfg("headless", True)))
        inject_domain_cookies(driver, LOGIN_HOME, cookies)
        result["done"], result["skipped"] = run_qa_work(
            ctx, driver, work, full_auto=full_auto)
    except Exception as exc:
        result["error"] = str(exc)
        ctx.log(f"并行答题与互动线程失败: {exc}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def run_combined(ctx: PluginContext, driver) -> None:
    cache = load_task_cache(ctx)
    if not cache.get("bdata") and not cache.get("spoc"):
        raise RuntimeError(
            "尚未获取课程详情，请先点击“获取课程详情”")
    auto_answer = bool(ctx.cfg("auto_answer", False))
    comment_reply = bool(ctx.cfg("comment_reply", False))
    full_auto = bool(ctx.cfg("full_auto", False)) and (
        auto_answer or comment_reply)
    jobs, quiz_items, interaction_items = build_unified_work(
        cache,
        ctx.cfg("course_plans", {}),
        auto_answer,
        comment_reply,
    )
    total_qa = len(quiz_items) + len(interaction_items)
    if not jobs and not total_qa:
        ctx.progress(done=0, total=0)
        ctx.status("所选范围内没有需要处理的内容", done=True)
        return
    spoc_count = sum(1 for job in jobs if job["platform"] == "spoc")
    bdata_count = len(jobs) - spoc_count
    ctx.log(f"课程任务开始: SPOC 视频 {spoc_count}, 师道云视频 {bdata_count}")
    if total_qa:
        ctx.log(f"并行答题与互动: {total_qa} 项")
    if full_auto:
        ctx.log("警告: 全自动模式已启用，答题和互动内容将不经人工核验直接提交")

    qa_result: dict = {}
    qa_thread = None
    qa_work: list[tuple[tuple[str, str], str, dict | str, str]] = []
    if auto_answer:
        qa_work.extend(
            (
                (item["course_url"], ""), item["course_title"],
                item["title"], item.get("xpath", ""))
            for item in quiz_items
        )
    if comment_reply:
        qa_work.extend(
            (
                (item["course_url"], ""), item["course_title"],
                item["group"], "")
            for item in interaction_items
        )

    platforms = {job["platform"] for job in jobs}
    if qa_work:
        platforms.add("bdata")
    if "bdata" in platforms:
        ctx.status("正在登录师道云…")
        ensure_authenticated(ctx, driver, discover_courses=False)
    if "spoc" in platforms:
        ctx.status("正在登录 SPOC…")
        spoc_platform.ensure_login(ctx, driver, load_cookies(ctx))
    if platforms:
        save_cookies(ctx, driver.get_cookies())
    cookie_set = load_cookies(ctx) or driver.get_cookies()

    if qa_work and jobs:
        if not str(ctx.cfg("api_key") or "").strip():
            raise RuntimeError(
                "自动答题或评论回复已启用，但未配置模型 API Key")
        qa_thread = threading.Thread(
            target=run_qa_worker,
            args=(ctx, qa_work, cookie_set, full_auto, qa_result),
            daemon=True,
            name="bnu-qa-worker",
        )
        qa_thread.start()
    elif qa_work and not str(ctx.cfg("api_key") or "").strip():
        raise RuntimeError(
            "自动答题或评论回复已启用，但未配置模型 API Key")

    tracker = None
    if jobs:
        workers = min(max(1, positive_int(ctx.cfg("workers", 2), 2)), len(jobs))
        ctx.log(
            f"开始混合播放视频: {len(jobs)} 个, {workers} 并发")
        tracker = run_parallel(
            ctx,
            jobs,
            cookie_set,
            driver,
            bounded_float(ctx.cfg("speedup", 3), 3.0, 1.0, 3.0),
            bounded_float(
                ctx.cfg("completion_tolerance", 3), 3.0, 0.0, 30.0),
            bool(ctx.cfg("only_unfinished", True)),
            workers,
        )
    if not jobs and qa_work:
        result: dict = {}
        run_qa_worker(ctx, qa_work, cookie_set, full_auto, result)
        qa_thread = None
        qa_result.update(result)
    if qa_thread is not None:
        qa_thread.join()

    video_done = sum(
        1 for status in (tracker.results.values() if tracker else [])
        if status == "已完成")
    qa_done = int(qa_result.get("done", 0))
    qa_skipped = int(qa_result.get("skipped", 0))
    if qa_result.get("error"):
        ctx.log(f"并行答题线程错误: {qa_result['error']}")
    ctx.progress(
        done=video_done,
        total=len(jobs),
        workers=tracker.snapshot()["workers"] if tracker else {},
    )
    ctx.status(
        f"BNU刷课结束: 视频完成 {video_done}/{len(jobs)}，"
        f"答题互动完成 {qa_done}/{total_qa}，"
        f"答题互动跳过或失败 {qa_skipped}",
        done=True)


def main() -> None:
    ctx = PluginContext()
    if not ctx.username or not ctx.password:
        ctx.log("未配置全局统一认证账号，退出")
        return

    task_mode = str(
        ctx.cfg("task_mode", "combined") or "combined").strip().lower()
    if task_mode not in {"crawl", "combined"}:
        task_mode = "combined"
    headless = bool(ctx.cfg("headless", True))

    driver = make_driver(ctx, headless=headless)
    driver.set_page_load_timeout(60)
    try:
        if task_mode == "combined":
            run_combined(ctx, driver)
            return

        bdata_courses: list[dict] = []
        spoc_ready = False
        try:
            bdata_courses = ensure_authenticated(ctx, driver)
        except (RuntimeError, WebDriverException) as exc:
            ctx.log(f"师道云登录或课程发现失败: {exc}")
        cached = load_cookies(ctx)
        try:
            spoc_platform.ensure_login(ctx, driver, cached)
            spoc_ready = True
        except (RuntimeError, WebDriverException) as exc:
            ctx.log(f"SPOC 登录失败: {exc}")
        save_cookies(ctx, driver.get_cookies())

        cache = load_task_cache(ctx)
        bdata_ok = False
        if bdata_courses:
            try:
                cache = refresh_task_cache(
                    ctx, driver, bdata_courses)
                bdata_ok = True
            except (RuntimeError, WebDriverException) as exc:
                ctx.log(f"师道云课程缓存失败: {exc}")
        if spoc_ready:
            cache, _spoc_count, spoc_failed = refresh_spoc_cache(
                ctx, driver)
        else:
            spoc_failed = 1
        if not bdata_ok and not spoc_ready:
            raise RuntimeError("SPOC 和师道云课程详情均获取失败")
        bdata_count = len(cache.get("bdata", {}))
        spoc_count = len(cache.get("spoc", []))
        task_count = sum(
            len(course.get("tasks", []))
            for course in cache.get("bdata", {}).values()
            if isinstance(course, dict))
        ctx.status(
            f"课程详情已更新: 师道云 {bdata_count} 门/"
            f"{task_count} 个任务，SPOC {spoc_count} 门课程，"
            f"失败平台任务 {spoc_failed}",
            done=True)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
