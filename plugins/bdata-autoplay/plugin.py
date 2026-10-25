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
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _sdk.sdk import (  # noqa: E402
    PluginContext,
    make_driver,
    find,
    find_strict,
)
from selenium.common.exceptions import WebDriverException  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402

LOGIN_HOME = "https://bdata.bnu.edu.cn/"
COURSE_DATA_API = "/wechatapi/api/learn/getCourseData"
COOKIE_LIFE_SECONDS = 7 * 86400

TASK_BUTTON_XPATH = (
    '//*[@id="app"]/section/main/div/div[2]/div[2]/div/div/div[3]'
    '/span/button[1]'
)
TASK_VIDEO_ICON_PREFIX = "M512 64a448"
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


def collect_task_videos(driver) -> list[dict]:
    """Open the task dialog and collect required videos by their play icon."""
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
        button.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", button)
    find_strict(driver, '//*[@id="pane-first"]', timeout=15)

    collected: dict[str, dict] = {}
    for _ in range(30):
        for row in _task_video_entries(driver):
            title = normalize_title(row.get("title"))
            if title:
                collected[title] = {
                    "title": title,
                    "watched": normalize_title(row.get("watched")),
                }
        if not _scroll_task_pane(driver):
            break
        time.sleep(0.15)
    close_task_dialog(driver)
    return list(collected.values())


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
            time.sleep(0.1)
    raise RuntimeError("任务进度窗口未能关闭")


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
    local_ended_at: float | None = None

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
                })

        if state.get("ended"):
            local_ended_at = local_ended_at or now
        else:
            local_ended_at = None
        if current <= last_media + 0.05:
            stalled_rounds += 1
        else:
            stalled_rounds = 0
            last_media = current
        if stalled_rounds >= 15 and not state.get("ended"):
            start_or_resume_video(driver, server_watched, speedup)
            stalled_rounds = 0
        if local_ended_at is not None and now - local_ended_at > 30:
            break
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
            tracker.set_worker(
                worker_id, status="检查中", title=title, percent=0.0,
                eta_s=None, total_s=None, course=job["course_title"])
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

                def report(fields: dict) -> None:
                    tracker.set_worker(
                        worker_id,
                        status="播放中",
                        title=fields["title"],
                        percent=fields["percent"],
                        eta_s=fields["eta_s"],
                        total_s=round(fields["target_seconds"]),
                        course=job["course_title"],
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

    speedup = bounded_float(ctx.cfg("speedup", 3), 3.0, 1.0, 3.0)
    tolerance = bounded_float(ctx.cfg("completion_tolerance", 3), 3.0, 0.0, 30.0)
    worker_count = int(bounded_float(ctx.cfg("workers", 2), 2.0, 1.0, 4.0))
    only_unfinished = bool(ctx.cfg("only_unfinished", True))
    headless = bool(ctx.cfg("headless", True))

    driver = make_driver(ctx, headless=headless)
    driver.set_page_load_timeout(60)
    try:
        first_url, first_cid = courses[0]
        ensure_authenticated(ctx, driver, first_url, first_cid)
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
