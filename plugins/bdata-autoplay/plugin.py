"""师道云课程视频自动播放插件。

站点关键行为(2026-10 实测):
- 未登录时课程页提供“北师大统一身份认证登录”入口, 点击后进入 CAS;
- 课程数据 /wechatapi/api/learn/getCourseData 返回目录, pid.type=2 为视频;
- /wechatapi/api/learn/getHistory 提供每个内容项的 needFinishvideo 秒数阈值;
- /wechatapi/api/learn/getVideoHistory 的 vt 为服务器已记录观看秒数;
- 视频以 4x 播放 8 秒后, vt 与实际媒体进度同步增长, 可据此判定真实完成度。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
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
HISTORY_API = "/wechatapi/api/learn/getHistory"
VIDEO_HISTORY_API = "/wechatapi/api/learn/getVideoHistory"
VIDEO_PART_TYPE = 2
COOKIE_LIFE_SECONDS = 7 * 86400

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


def video_parts(course_data: dict) -> list[dict]:
    """Return visible, non-hidden video entries from getCourseData."""
    hidden = {str(v) for v in (course_data.get("hide") or [])}
    out: list[dict] = []
    seen: set[str] = set()
    for item in course_data.get("cs") or []:
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        cs_id = str(item.get("_id") or "")
        if not isinstance(pid, dict) or not cs_id or cs_id in seen:
            continue
        part_id = str(pid.get("_id") or "")
        title = normalize_title(pid.get("title"))
        if pid.get("type") != VIDEO_PART_TYPE or not part_id or not title:
            continue
        if cs_id in hidden or part_id in hidden:
            continue
        seen.add(cs_id)
        out.append({"cs_id": cs_id, "part_id": part_id, "title": title})
    return out


def completion_target(required: float, media_duration: float) -> float:
    """Use the smaller positive course/media duration to handle rounded page tags."""
    values = [float(v) for v in (required, media_duration) if float(v or 0) > 0]
    return min(values) if values else 0.0


def bounded_float(value: object, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


def is_complete(watched: float, required: float, media_duration: float,
                tolerance: float) -> bool:
    target = completion_target(required, media_duration)
    return target > 0 and watched + max(0.0, tolerance) >= target


def cookie_file(ctx: PluginContext) -> Path:
    return Path(ctx.datadir) / "cookies.json"


def save_cookies(ctx: PluginContext, cookies: list[dict]) -> None:
    path = cookie_file(ctx)
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


def course_history(driver, cid: str) -> dict:
    return api_json(driver, HISTORY_API, {"cid": cid})


def video_history(driver, cid: str, cs_id: str) -> float:
    data = api_json(driver, VIDEO_HISTORY_API, {"cid": cid, "csId": cs_id})
    try:
        return max(0.0, float(data.get("vt") or 0))
    except (TypeError, ValueError):
        return 0.0


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


def wait_current_part(driver, cid: str, cs_id: str, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            history = course_history(driver, cid).get("history") or {}
            if str(history.get("csId") or "") == cs_id:
                return
        except Exception as e:
            last_error = e
        time.sleep(0.4)
    raise RuntimeError(f"切换课程内容超时: {cs_id} ({last_error or '条目未激活'})")


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


def play_until_complete(ctx: PluginContext, driver, cid: str, part: dict,
                        required: float, watched: float, speedup: float,
                        tolerance: float, overall_done: int,
                        overall_total: int) -> tuple[bool, float, float]:
    deadline_wait = time.time() + 20
    state: dict = {}
    while time.time() < deadline_wait:
        state = video_state(driver)
        if float(state.get("duration") or 0) > 0:
            break
        time.sleep(0.4)
    if not state or not state.get("duration"):
        return False, watched, 0.0

    media_duration = float(state["duration"])
    required = completion_target(required, media_duration)
    if required <= 0:
        return False, watched, media_duration
    if watched + tolerance >= required:
        return True, watched, media_duration

    start_or_resume_video(driver, watched, speedup)
    started = time.time()
    max_wait = max(120.0, (required - watched) / max(1.0, speedup) * 2.0 + 120.0)
    last_server_check = 0.0
    last_report = 0.0
    last_media = -1.0
    stalled_rounds = 0
    server_watched = watched

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
        if now - last_server_check >= 4:
            last_server_check = now
            try:
                server_watched = video_history(driver, cid, part["cs_id"])
            except RuntimeError:
                pass

        target = completion_target(required, media_duration)
        if server_watched + tolerance >= target:
            driver.execute_script(
                "const v=document.querySelector('#player video, video');"
                "if(v){v.pause();}")
            return True, server_watched, media_duration

        if now - last_report >= 2:
            last_report = now
            percent = min(100.0, server_watched / target * 100) if target else 0.0
            remain = max(0.0, target - server_watched) / max(1.0, speedup)
            ctx.progress(
                done=overall_done, total=overall_total, title=part["title"],
                watched_seconds=round(server_watched, 1),
                required_seconds=round(target, 1),
                percent=round(percent, 1), eta_seconds=round(remain),
            )

        if current <= last_media + 0.05:
            stalled_rounds += 1
        else:
            stalled_rounds = 0
            last_media = current
        if stalled_rounds >= 15:
            start_or_resume_video(driver, server_watched, speedup)
            stalled_rounds = 0
        time.sleep(1.0)

    try:
        server_watched = video_history(driver, cid, part["cs_id"])
    except RuntimeError:
        pass
    return is_complete(server_watched, required, media_duration, tolerance), \
        server_watched, media_duration


def run_course(ctx: PluginContext, driver, course_url: str, cid: str,
               only_unfinished: bool, speedup: float, tolerance: float,
               course_index: int, course_total: int) -> tuple[int, int]:
    driver.get(course_url)
    data = course_data(driver, cid)
    title = normalize_title(data.get("title")) or cid
    history = course_history(driver, cid)
    thresholds = history.get("newTaskData") or {}
    parts = video_parts(data)
    ctx.log(f"[{course_index}/{course_total}] {title}: 发现 {len(parts)} 个视频")
    ctx.progress(done=0, total=len(parts), course=title)

    completed = 0
    for index, part in enumerate(parts, start=1):
        ctx.status(f"正在检查: {part['title']}")
        try:
            watched = video_history(driver, cid, part["cs_id"])
            task = thresholds.get(part["cs_id"]) or {}
            try:
                required = float(task.get("needFinishvideo") or 0)
            except (TypeError, ValueError):
                required = 0.0

            if not click_menu_title(driver, part["title"]):
                ctx.log(f"跳过(目录中未找到): {part['title']}")
                continue
            wait_current_part(driver, cid, part["cs_id"])
            time.sleep(0.8)

            media_duration = float(video_state(driver).get("duration") or 0)
            page_label = wait_duration_label(driver)
            page_required = parse_duration_seconds(page_label)
            # 部分交互视频的接口阈值是 1 秒哨兵值, 页面时长和媒体时长更可靠。
            api_required = required if required > 2 else 0.0
            effective_required = page_required or api_required or media_duration
            target = completion_target(effective_required, media_duration)
            page_complete = "已完成" in page_label
            if only_unfinished and (page_complete or is_complete(
                    watched, effective_required, media_duration, tolerance)):
                ctx.log(
                    f"跳过已完成视频({watched:.0f}/{target:.0f}s): {part['title']}")
            else:
                ok, watched, media_duration = play_until_complete(
                    ctx, driver, cid, part, effective_required, watched, speedup,
                    tolerance, completed, len(parts))
                if not ok:
                    raise RuntimeError(
                        f"播放未达到完成阈值({watched:.0f}s): {part['title']}")

            completed += 1
            ctx.status(
                f"完成 {index}/{len(parts)}: {part['title']}",
                watched_seconds=round(watched, 1),
            )
            ctx.progress(
                done=completed, total=len(parts), title=part["title"],
                watched_seconds=round(watched, 1),
                required_seconds=round(
                    completion_target(effective_required, media_duration), 1),
            )
        except Exception as e:
            ctx.log(f"视频处理失败: {part['title']}: {e!r}")
            ctx.progress(done=completed, total=len(parts), title=part["title"])
    return completed, len(parts)


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

    speedup = bounded_float(ctx.cfg("speedup", 8), 8.0, 1.0, 16.0)
    tolerance = bounded_float(ctx.cfg("completion_tolerance", 3), 3.0, 0.0, 30.0)
    only_unfinished = bool(ctx.cfg("only_unfinished", True))
    headless = bool(ctx.cfg("headless", True))

    driver = make_driver(ctx, headless=headless)
    driver.set_page_load_timeout(60)
    try:
        first_url, first_cid = courses[0]
        ensure_authenticated(ctx, driver, first_url, first_cid)
        total_completed = 0
        total_videos = 0
        for index, (url, cid) in enumerate(courses, start=1):
            try:
                completed, total = run_course(
                    ctx, driver, url, cid, only_unfinished, speedup,
                    tolerance, index, len(courses))
                total_completed += completed
                total_videos += total
            except Exception as e:
                ctx.log(f"课程处理失败({cid}): {e!r}")
        ctx.progress(done=total_completed, total=total_videos)
        ctx.status(
            f"师道云刷课处理结束: {total_completed}/{total_videos} 个视频",
            done=True)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
