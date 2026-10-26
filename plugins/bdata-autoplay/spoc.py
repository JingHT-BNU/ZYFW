"""SPOC course discovery and video playback helpers for BNU刷课."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from _sdk.sdk import ANTI_PAUSE_JS, speed_hijack_js, speed_keeper_js

LOGIN_HOME = "https://spoc.bnu.edu.cn/"
COURSELIST_URL = "https://spoc.bnu.edu.cn/pro/courselist"
CHAPTER_API = (
    "https://spoc.bnu.edu.cn/mooc-api/v1/lms/learn/course/chapter"
    "?cid={cid}&sign={sign}&term=latest&uv_id=2600"
)
SCHEDULE_API = (
    "https://spoc.bnu.edu.cn/mooc-api/v1/lms/learn/course/schedule"
    "?cid={cid}&sign={sign}&term=latest&uv_id=2600"
)
VIDEO_PAGE = "https://spoc.bnu.edu.cn/pro/lms/{sign}/{cid}/video/{leaf_id}"
STUDY_URL_RE = re.compile(r"/pro/lms/([^/]+)/([^/]+)/studycontent")
VIDEO_URL_RE = re.compile(r"/pro/lms/([^/]+)/([^/]+)/video/([^/?#]+)")
STATUS_EXACT_XPATH = (
    '//*[@id="app"]/div/div[2]/div[2]/div/div/div[1]'
    '/div[1]/div[2]/span/span[1]'
)
STATUS_LOOSE_XPATH = (
    "//*[@class and contains(@class,'progress-wrap')]"
    "//span[contains(@class,'text')]"
)
PLAY_BTN_XPATH = '//*[@id="video-box"]/div/xt-wrap/xt-bigbutton/button'
TIME_TOTAL_XPATH = (
    '//*[@id="video-box"]/div/xt-wrap/xt-controls/xt-inner'
    '/xt-time/span[2]'
)
NO_CONTENT_TEXT = "老师没有发布学习内容"
VIDEO_CLASS = "icon--shipin"
ASSIGNMENT_CLASSES = ("icon--zuoye", "icon--kaoshi")
XTBZ_HEADERS = {"xtbz": "ykt"}

_FETCH_JSON_JS = """
const done = arguments[arguments.length - 1];
fetch(arguments[0], {credentials: 'include', headers: arguments[1]})
  .then(async response => {
    const text = await response.text();
    let data;
    try { data = JSON.parse(text); }
    catch (error) { data = {error: text || ('HTTP ' + response.status)}; }
    done({status: response.status, data});
  })
  .catch(error => done({status: 0, data: {error: String(error)}}));
"""


@dataclass
class CourseCard:
    index: int
    title: str
    subtitle: str


def normalize(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\xa0", " ")).strip()


def _click(driver, element) -> None:
    try:
        element.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", element)


def is_logged_in(driver) -> bool:
    try:
        driver.get(COURSELIST_URL)
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#pane-student .studentCol")))
        return True
    except Exception:
        return False


def login(ctx, driver) -> list[dict]:
    """Log in through the BNU CAS flow used by the SPOC portal."""
    ctx.log("正在登录 SPOC 统一身份认证…")
    driver.get(LOGIN_HOME)
    time.sleep(3)
    buttons = driver.find_elements(By.CSS_SELECTOR, ".login-btn")
    if not buttons:
        raise RuntimeError("SPOC 页面未找到登录按钮")
    _click(driver, buttons[0])
    time.sleep(1)

    selected = False
    for element in driver.find_elements(
            By.CSS_SELECTOR, ".login-dialog .name, .login-dialog .txt"):
        if normalize(element.text) == "北京师范大学":
            _click(driver, element)
            selected = True
            break
    if not selected:
        raise RuntimeError("未找到北京师范大学统一认证入口")

    time.sleep(1)
    for element in driver.find_elements(
            By.XPATH, "//*[contains(text(),'前往认证')]"):
        _click(driver, element)
        break

    def find_input(hints: tuple[str, ...]):
        for element in driver.find_elements(By.TAG_NAME, "input"):
            placeholder = element.get_attribute("placeholder") or ""
            if any(hint in placeholder for hint in hints):
                return element
        return None

    clicked_login = time.time()
    credentials_submitted = False
    last_login_probe = 0.0
    deadline = time.time() + 35
    while time.time() < deadline:
        user_input = find_input(("学工号", "学号", "用户名", "邮箱"))
        password_input = find_input(("密码",))
        if user_input is not None and password_input is not None:
            user_input.clear()
            user_input.send_keys(ctx.username)
            password_input.clear()
            password_input.send_keys(ctx.password)
            for element in driver.find_elements(
                    By.XPATH,
                    "//button[contains(.,'登录')] | //a[contains(.,'登录')] "
                    "| //input[@type='submit']"):
                _click(driver, element)
                credentials_submitted = True
                break
            if credentials_submitted:
                break
        if ("spoc.bnu.edu.cn" in driver.current_url
                and time.time() - clicked_login > 3
                and time.time() - last_login_probe > 5
                and is_logged_in(driver)):
            ctx.log("SPOC 统一身份认证登录成功")
            return driver.get_cookies()
        if "spoc.bnu.edu.cn" in driver.current_url:
            last_login_probe = time.time()
        time.sleep(0.5)

    if not credentials_submitted and not is_logged_in(driver):
        raise RuntimeError("SPOC 统一身份认证页面未显示账号输入框")

    deadline = time.time() + 60
    while time.time() < deadline:
        if "spoc.bnu.edu.cn" in driver.current_url:
            if is_logged_in(driver):
                ctx.log("SPOC 统一身份认证登录成功")
                return driver.get_cookies()
        time.sleep(0.5)
    raise RuntimeError("SPOC 登录超时，请检查全局统一认证账号密码")


def ensure_login(ctx, driver, cached_cookies: list[dict] | None = None) -> list[dict]:
    driver.get(LOGIN_HOME)
    for cookie in cached_cookies or []:
        try:
            driver.add_cookie(cookie)
        except WebDriverException:
            pass
    if is_logged_in(driver):
        ctx.log("使用缓存 Cookie 登录 SPOC")
        return driver.get_cookies()
    return login(ctx, driver)


def parse_study_url(url: str) -> tuple[str, str] | None:
    match = STUDY_URL_RE.search(str(url or ""))
    return (match.group(1), match.group(2)) if match else None


def parse_video_url(url: str) -> tuple[str, str, str] | None:
    match = VIDEO_URL_RE.search(str(url or ""))
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _fetch_json(driver, url: str) -> dict:
    driver.set_script_timeout(30)
    result = driver.execute_async_script(_FETCH_JSON_JS, url, XTBZ_HEADERS)
    if not isinstance(result, dict):
        raise RuntimeError("SPOC 接口返回格式异常")
    body = result.get("data")
    if not isinstance(body, dict):
        raise RuntimeError("SPOC 接口返回非 JSON 数据")
    if result.get("status") != 200 or body.get("error"):
        raise RuntimeError(
            f"SPOC 接口请求失败({result.get('status')}): "
            f"{body.get('msg') or body.get('error') or '会话已失效'}")
    return body


def list_leaves(driver, sign: str, cid: str) -> list[dict]:
    body = _fetch_json(driver, CHAPTER_API.format(cid=cid, sign=sign))
    if not body.get("success"):
        raise RuntimeError(
            f"SPOC 章节接口失败: {body.get('msg') or '未知错误'}")
    leaves: list[dict] = []
    for chapter in (body.get("data") or {}).get("course_chapter", []):
        chapter_name = normalize(chapter.get("name"))
        for section in chapter.get("section_leaf_list", []):
            values = section.get("leaf_list")
            if not values and section.get("leaf_type") is not None:
                values = [section]
            for leaf in values or []:
                if not isinstance(leaf, dict) or leaf.get("id") is None:
                    continue
                leaves.append({
                    "id": leaf["id"],
                    "name": normalize(leaf.get("name")),
                    "leaf_type": leaf.get("leaf_type"),
                    "chapter": chapter_name,
                    "locked": bool(leaf.get("is_locked")),
                    "shown": leaf.get("is_show", True) is not False,
                })
    return leaves


def fetch_schedule(driver, sign: str, cid: str) -> dict:
    body = _fetch_json(driver, SCHEDULE_API.format(cid=cid, sign=sign))
    data = body.get("data")
    return (data.get("leaf_schedules") or {}) if isinstance(data, dict) else {}


def leaf_progress(schedule: dict, leaf_id: object) -> tuple[bool, float]:
    value = schedule.get(str(leaf_id))
    if isinstance(value, dict):
        total = float(value.get("total") or 1)
        done = float(value.get("done") or 0)
        return done >= total, min(100.0, done / total * 100) if total else 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number >= 1, min(100.0, max(0.0, number * 100))
    return False, 0.0


def _card_metadata(driver) -> list[CourseCard]:
    cards = driver.find_elements(
        By.CSS_SELECTOR, "#pane-student .studentCol")
    result: list[CourseCard] = []
    for index, card in enumerate(cards):
        headings = card.find_elements(By.TAG_NAME, "h1")
        lines = [normalize(line) for line in (card.text or "").splitlines()
                 if normalize(line)]
        title = normalize(headings[0].text) if headings else (
            lines[0] if lines else f"课程 {index + 1}")
        subtitle = lines[1] if len(lines) > 1 else ""
        result.append(CourseCard(index, title, subtitle))
    return result


def prepare_course_list(driver) -> list[CourseCard]:
    driver.get(COURSELIST_URL)
    deadline = time.time() + 25
    while time.time() < deadline:
        if driver.find_elements(
                By.CSS_SELECTOR, "#pane-student .studentCol"):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("SPOC 课程列表加载超时")
    all_terms = driver.find_elements(
        By.XPATH, '//*[@id="pane-student"]/div[1]/div[2]/button')
    if all_terms:
        _click(driver, all_terms[0])
        time.sleep(1.5)
    return _card_metadata(driver)


def open_course_card(driver, index: int, expected_title: str = ""
                     ) -> tuple[str, str, str]:
    """Open one course card and return (url, sign, cid)."""
    cards = _card_metadata(driver)
    target_index = index
    if expected_title:
        for position, card in enumerate(cards):
            if card.title == expected_title:
                target_index = position
                break
    if target_index >= len(cards):
        raise RuntimeError("SPOC 课程卡片已变化")
    elements = driver.find_elements(
        By.CSS_SELECTOR, "#pane-student .studentCol")
    headings = elements[target_index].find_elements(By.TAG_NAME, "h1")
    target = headings[0] if headings else elements[target_index]
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});"
        "arguments[0].click();", target)
    deadline = time.time() + 20
    while time.time() < deadline:
        match = STUDY_URL_RE.search(driver.current_url)
        if match:
            return driver.current_url, match.group(1), match.group(2)
        time.sleep(0.4)
    raise RuntimeError(f"未能打开 SPOC 课程: {cards[target_index].title}")


def _dom_item_icons(driver) -> dict[str, str]:
    rows = driver.execute_script(
        """
        const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
        return Array.from(document.querySelectorAll(
            '.chapter-list .leaf-detail')).map(row => {
          const icon = row.querySelector('.iconfont');
          const title = row.querySelector('.leaf-title');
          return {
            title: norm(title && title.textContent),
            icon: icon ? String(icon.className || '') : ''
          };
        });
        """
    )
    if not isinstance(rows, list):
        return {}
    return {
        normalize(row.get("title")): str(row.get("icon") or "")
        for row in rows if normalize(row.get("title"))
    }


def cache_course(ctx, driver, card: CourseCard, url: str,
                 sign: str, cid: str) -> dict:
    time.sleep(0.8)
    body_text = normalize(driver.find_element(By.TAG_NAME, "body").text)
    course = {
        "name": card.title,
        "url": url,
        "chapters": {},
    }
    if NO_CONTENT_TEXT in body_text:
        ctx.log(f"SPOC 无学习内容: {card.title}")
        return course

    leaves = list_leaves(driver, sign, cid)
    schedule = fetch_schedule(driver, sign, cid)
    icons = _dom_item_icons(driver)
    chapters: dict[str, dict] = {}
    video_count = 0
    exam_count = 0
    for leaf in leaves:
        if not leaf.get("shown") or leaf.get("locked"):
            continue
        icon = icons.get(leaf["name"], "")
        completed, _percent = leaf_progress(schedule, leaf["id"])
        is_video = (
            VIDEO_CLASS in icon or leaf.get("leaf_type") == 0)
        chapter_name = leaf["chapter"] or "未分章"
        chapter = chapters.setdefault(chapter_name, {
            "name": chapter_name,
            "videos": {},
            "exams": {},
        })
        if is_video:
            video_key = str(len(chapter["videos"]))
            chapter["videos"][video_key] = {
                "name": leaf["name"],
                "url": VIDEO_PAGE.format(
                    sign=sign, cid=cid, leaf_id=leaf["id"]),
                "finished": completed,
            }
            video_count += 1
        else:
            is_assignment = (
                any(marker in icon for marker in ASSIGNMENT_CLASSES)
                or leaf.get("leaf_type") != 0
                or any(word in leaf["name"] for word in
                       ("作业", "测试", "练习", "考试"))
            )
            if is_assignment:
                exam_key = str(len(chapter["exams"]))
                chapter["exams"][exam_key] = {
                    "name": leaf["name"],
                    "url": (
                        "https://spoc.bnu.edu.cn/ai-workspace/lms-graph/"
                        f"{cid}/quiz/{leaf['id']}"
                    ),
                    "finished": completed,
                }
                exam_count += 1

    course["chapters"] = {
        str(index): chapter
        for index, chapter in enumerate(chapters.values())
        if chapter["videos"]
    }
    ctx.log(
        f"已缓存 SPOC 课程 {card.title}: {video_count} 个视频, "
        f"{exam_count} 个作业/测试")
    return course


def cache_all_courses(ctx, driver, progress: Callable[[int, int, str], None],
                      save_course: Callable[[dict], None]) -> int:
    cards = prepare_course_list(driver)
    if not cards:
        return 0
    failed = 0
    for position, card in enumerate(cards, start=1):
        progress(position - 1, len(cards), card.title)
        try:
            prepare_course_list(driver)
            url, sign, cid = open_course_card(
                driver, card.index, expected_title=card.title)
            course = cache_course(ctx, driver, card, url, sign, cid)
            save_course(course)
        except (RuntimeError, WebDriverException) as exc:
            failed += 1
            ctx.log(f"SPOC 课程缓存失败({card.title}): {exc}")
    progress(len(cards), len(cards), "SPOC 课程缓存完成")
    return failed


def _parse_time(text: str) -> float:
    match = re.search(r"(?:(\d+):)?(\d{1,2}):(\d{2})\s*$", text or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)


def _read_total_seconds(driver) -> float:
    try:
        elements = driver.find_elements(By.XPATH, TIME_TOTAL_XPATH)
        if elements:
            value = _parse_time(elements[0].text)
            if value > 0:
                return value
        value = driver.execute_script(
            "const v=document.querySelector('#video-box video');"
            "return v && isFinite(v.duration) ? v.duration : 0;")
        return float(value or 0)
    except WebDriverException:
        return 0.0


def _read_status_text(driver) -> str:
    for xpath in (STATUS_EXACT_XPATH, STATUS_LOOSE_XPATH):
        try:
            for element in driver.find_elements(By.XPATH, xpath):
                text = normalize(element.text)
                if text:
                    return text
        except WebDriverException:
            continue
    return ""


def play_video(driver, sign: str, cid: str, leaf_id: object,
               speedup: float, only_unfinished: bool = True,
               on_progress=None) -> str:
    driver.get(VIDEO_PAGE.format(sign=sign, cid=cid, leaf_id=leaf_id))
    try:
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.TAG_NAME, "video")))
    except Exception:
        return "页面无播放器"

    duration = 0.0
    for _ in range(8):
        duration = _read_total_seconds(driver)
        if duration > 0:
            break
        time.sleep(1)
    if only_unfinished and _read_status_text(driver) == "已完成":
        if on_progress:
            on_progress(100.0, 0, duration)
        return "已完成"

    driver.execute_script(
        "const v=document.querySelector('#video-box video');"
        "if(v && v.duration && v.currentTime > v.duration - 1)"
        " v.currentTime = 0;")
    try:
        button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, PLAY_BTN_XPATH)))
        button.click()
    except Exception:
        return "找不到播放按钮"

    driver.execute_script(ANTI_PAUSE_JS)
    driver.execute_script(speed_hijack_js(speedup))
    driver.execute_script(speed_keeper_js(speedup))
    started = time.time()
    last_report = 0.0
    deadline = time.time() + 3 * 3600
    poll_api = time.time() + 60
    api_confirmed_at: float | None = None
    while time.time() < deadline:
        status_text = _read_status_text(driver)
        if status_text == "已完成":
            if on_progress:
                on_progress(100.0, 0, duration)
            return "已完成"
        played = (time.time() - started) * speedup
        try:
            current = driver.execute_script(
                "const v=document.querySelector('#video-box video');"
                "return v ? v.currentTime : null;")
            if isinstance(current, (int, float)):
                played = float(current)
        except WebDriverException:
            pass
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", status_text)
        percent = float(match.group(1)) if match else (
            min(99.0, played / duration * 100) if duration > 0 else 0.0)
        remains = max(0.0, duration - played) / speedup if duration > 0 else None
        if on_progress and time.time() - last_report >= 2:
            last_report = time.time()
            on_progress(percent, remains, duration)

        if api_confirmed_at is None and time.time() >= poll_api:
            poll_api = time.time() + 60
            try:
                complete, _ = leaf_progress(
                    fetch_schedule(driver, sign, cid), leaf_id)
                if complete:
                    api_confirmed_at = time.time()
            except Exception:
                pass
        if api_confirmed_at and time.time() - api_confirmed_at > 120:
            return "服务器已记录"
        time.sleep(5)
    return "等待超时"


def is_spoc_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    return (
        parsed.scheme == "https"
        and parsed.hostname == "spoc.bnu.edu.cn"
        and parsed.path.startswith("/pro/lms/")
    )
