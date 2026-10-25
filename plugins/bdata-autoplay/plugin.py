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
from selenium.webdriver.common.by import By  # noqa: E402

from llm import (  # noqa: E402
    LLMError,
    generate_interaction,
    generate_quiz_answers,
)

LOGIN_HOME = "https://bdata.bnu.edu.cn/"
COURSE_DATA_API = "/wechatapi/api/learn/getCourseData"
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
SURVEY_VALUES = (1, 1, 1, 3, 1)

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
    const dialog = document.querySelector('.el-dialog');
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
                close_task_dialog(driver)
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
        close_task_dialog(driver)
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


def close_task_dialog(driver) -> None:
    # 优先模拟点击遮罩空白处; 站点未关闭时回退到标题栏关闭按钮。
    blank_js = """
    const dialog = document.querySelector('.el-dialog');
    const overlay = dialog && dialog.closest('.el-overlay');
    if (!overlay) return false;
    for (const type of ['mousedown', 'mouseup', 'click']) {
        overlay.dispatchEvent(new MouseEvent(type, {
            bubbles: true, cancelable: true, clientX: 5, clientY: 5
        }));
    }
    return true;
    """
    close_js = """
    const button = document.querySelector('.el-dialog__headerbtn');
    if (button) button.click();
    return true;
    """
    for action in (blank_js, close_js, close_js):
        try:
            driver.execute_script(action)
        except WebDriverException:
            pass
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not driver.find_elements(By.ID, "pane-first"):
                return
            if not _task_pane_ready(driver, require_cards=False):
                return
            time.sleep(0.1)
    raise RuntimeError("任务进度窗口未能关闭")


def read_quiz_fields(driver) -> list[dict]:
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
            for index, (label, value) in enumerate(
                    zip(SURVEY_QUESTIONS, SURVEY_VALUES, strict=True)):
                fields.append({
                    "key": f"{qid}_survey_{index}",
                    "label": f"量表 {index + 1}：{label}",
                    "type": "scale",
                    "options": [
                        {"value": str(i), "label": label_}
                        for i, label_ in enumerate(
                            ("非常不同意", "不同意", "一般", "同意", "非常同意"))
                    ],
                    "value": str(value),
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


def post_reply(driver, text: str) -> None:
    opened = driver.execute_script(
        """
        const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
        const first = document.querySelector('#commentBarScroll > ul > li');
        if (!first) return false;
        first.scrollIntoView({block: 'center'});
        const button = Array.from(first.querySelectorAll('button'))
            .find(el => norm(el.innerText) === '回复');
        if (!button) return false;
        button.click();
        return true;
        """
    )
    if not opened:
        raise RuntimeError("未找到第一条评论的回复按钮")
    deadline = time.time() + 5
    while time.time() < deadline:
        if _visible_editor(driver) is not None:
            break
        time.sleep(0.2)
    _enter_editor_text(driver, text)
    if not _click_visible_button(driver, "确认"):
        raise RuntimeError("未找到回复确认按钮")
    time.sleep(1.5)


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
        if field.get("key") in reviewed:
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
                  model: tuple[str, str, str]) -> bool:
    open_task_dialog(driver)
    click_task_entry(driver, test_title)
    fields = read_quiz_fields(driver)
    model_fields = [field for field in fields if field.get("type") != "scale"]
    ctx.status(f"正在生成答案: {test_title}")
    answers = generate_quiz_answers(
        *model, course_title, test_title, model_fields)
    for field in model_fields:
        answer = answers.get(field["key"], {})
        field["value"] = answer.get(
            "selected", "") if field["type"] == "single" else answer.get(
            "selected", []) if field["type"] == "multiple" else answer.get(
            "text", "")
    ctx.log(f"模型已生成 {test_title} 的答案，等待用户核验")
    response = broker.ask("quiz", f"测验：{test_title}", fields)
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
                          model: tuple[str, str, str]) -> bool:
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
    ctx.log(f"模型已生成 {video_title} 的互动内容，等待用户核验")
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

    task_mode = str(ctx.cfg("task_mode", "study") or "study").strip().lower()
    if task_mode not in {"study", "qa"}:
        task_mode = "study"
    if task_mode == "qa" and not str(ctx.cfg("api_key") or "").strip():
        ctx.log("答题与互动模式需要先在插件配置中填写模型 API Key,退出")
        return
    speedup = bounded_float(ctx.cfg("speedup", 3), 3.0, 1.0, 3.0)
    tolerance = bounded_float(ctx.cfg("completion_tolerance", 3), 3.0, 0.0, 30.0)
    worker_count = positive_int(ctx.cfg("workers", 2), 2)
    only_unfinished = bool(ctx.cfg("only_unfinished", True))
    headless = bool(ctx.cfg("headless", True))

    driver = make_driver(ctx, headless=headless)
    driver.set_page_load_timeout(60)
    try:
        first_url, first_cid = courses[0]
        ensure_authenticated(ctx, driver, first_url, first_cid)
        if task_mode == "qa":
            ctx.log("开始答题与互动任务")
            run_qa(ctx, driver, courses)
            return
        jobs = discover_course_jobs(ctx, driver, courses)
        if not jobs:
            ctx.progress(done=0, total=0)
            ctx.status("任务中没有需要播放的必修视频", done=True)
            return
        cookies = driver.get_cookies()
        workers = min(worker_count, len(jobs))
        ctx.log(f"开始并发刷课: {workers} 个浏览器进程, {len(jobs)} 个视频")
        tracker = run_parallel(
            ctx, jobs, cookies, driver, speedup, tolerance,
            only_unfinished, workers)
        snapshot = tracker.snapshot()
        completed = sum(
            1 for status in tracker.results.values() if status == "已完成")
        ctx.status(
            f"师道云刷课处理结束: "
            f"{completed}/{snapshot['total']} 个视频",
            done=True)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
