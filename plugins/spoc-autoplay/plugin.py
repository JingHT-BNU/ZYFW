"""SPOC 刷课插件 2.1:自动发现课程视频并多线程倍速刷课。

数据链路(全部经真实站点验证,2026-09-21):
- 登录:首页 .login-btn → 弹窗"北京师范大学" → "前往认证" → CAS 表单(placeholder 定位) → 回跳 SPOC
- 课程结构:GET /mooc-api/v1/lms/learn/course/chapter?cid={cid}&sign={sign} (需请求头 xtbz: ykt)
  返回章节树,叶子 leaf_type: 0=视频 6=练习 3=图文 4=其他
- 完成状态:GET /mooc-api/v1/lms/learn/course/schedule?cid={cid}&sign={sign}
  leaf_schedules: {leaf_id: 1 | {total, done}}
- 播放页:https://spoc.bnu.edu.cn/pro/lms/{sign}/{cid}/video/{leaf_id}
  (ai-workspace 新路由在自动化浏览器下不渲染,旧路由仍可用)
  播放必须用原生 element.click()(可信事件),JS click 会被自动播放策略拦截

2026-10-11 修订(完成判定以页面完成度元素为准):
- schedule 接口的 leaf 值可能是小数进度(0.0857 = 观看 8.57%),旧逻辑把它当
  布尔真值,导致"看过一点即视为已完成、实际服务器未记录"的误判 —— 已改为
  仅在播放页完成度元素显示"已完成"时才标记完成
- 未完成时同一元素显示"完成度:N%",N 即服务器真实进度,同时用于前端进度条
- 新版界面精确 XPATH(用户提供)://*[@id="app"]/div/div[2]/div[2]/div/div/
  div[1]/div[1]/div[2]/span/span[1];旧版播放路由下该绝对路径不存在(实测),
  用 class 语义匹配(progress-wrap / span.text)兜底,两者指向同一元素
- 视频总时长在点击播放前从 xt-time span[2](如 "00:10:33")读取,点击后该
  元素会被清空;剩余时间 = (总时长 - 已播视频秒数) / 倍速
"""
from __future__ import annotations

import json
import queue
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _sdk.sdk import (  # noqa: E402
    PluginContext, make_driver,
    speed_hijack_js, speed_keeper_js, ANTI_PAUSE_JS,
)
from selenium.common.exceptions import WebDriverException  # noqa: E402
from selenium.webdriver.support import expected_conditions as EC  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

LOGIN_HOME = "https://spoc.bnu.edu.cn/"
CHAPTER_API = ("https://spoc.bnu.edu.cn/mooc-api/v1/lms/learn/course/chapter"
               "?cid={cid}&sign={sign}&term=latest&uv_id=2600")
SCHEDULE_API = ("https://spoc.bnu.edu.cn/mooc-api/v1/lms/learn/course/schedule"
                "?cid={cid}&sign={sign}&term=latest&uv_id=2600")
COURSELIST_URL = "https://spoc.bnu.edu.cn/pro/courselist"
XTBZ = {"xtbz": "ykt"}
LEAF_TYPE_VIDEO = 0
STUDY_URL_RE = re.compile(r"/pro/lms/([^/]+)/([^/]+)/studycontent")
VIDEO_PAGE = "https://spoc.bnu.edu.cn/pro/lms/{sign}/{cid}/video/{leaf_id}"

# 完成度状态元素:新版界面精确路径(用户提供);旧版播放路由下绝对路径不同,
# 用 class 语义匹配兜底(实测两者命中同一元素)。未完成="完成度:N%",完成="已完成"
STATUS_EXACT_XPATH = '//*[@id="app"]/div/div[2]/div[2]/div/div/div[1]/div[1]/div[2]/span/span[1]'
STATUS_LOOSE_XPATH = (
    "//*[@class and contains(@class,'progress-wrap')]//span[contains(@class,'text')]")
PLAY_BTN_XPATH = '//*[@id="video-box"]/div/xt-wrap/xt-bigbutton/button'
TIME_TOTAL_XPATH = '//*[@id="video-box"]/div/xt-wrap/xt-controls/xt-inner/xt-time/span[2]'

FETCH_JSON_JS = """
const cb = arguments[arguments.length - 1];
fetch(arguments[0], {credentials: 'include', headers: arguments[1]})
  .then(r => r.json().then(j => cb(j)))
  .catch(e => cb({error: String(e)}));
"""

COOKIE_LIFE = 7 * 86400


# ---------------- 登录 ----------------

def do_login(ctx, driver) -> list:
    """走统一身份认证 CAS 链路登录,返回并缓存 cookies。"""
    ctx.log("执行统一身份认证登录...")
    driver.get(LOGIN_HOME)
    time.sleep(4)
    driver.find_element("css selector", ".login-btn").click()
    time.sleep(2)
    clicked = False
    for el in driver.find_elements("css selector", ".login-dialog .name, .login-dialog .txt"):
        if (el.text or "").strip() == "北京师范大学":
            driver.execute_script("arguments[0].click()", el)
            clicked = True
            break
    if not clicked:
        raise RuntimeError("未找到统一身份认证入口(北京师范大学)")
    time.sleep(2)
    for el in driver.find_elements("xpath", "//*[contains(text(),'前往认证')]"):
        driver.execute_script("arguments[0].click()", el)
        break
    time.sleep(4)

    def fill(ph_hints, value) -> bool:
        for el in driver.find_elements("tag name", "input"):
            ph = el.get_attribute("placeholder") or ""
            if any(h in ph for h in ph_hints):
                el.clear()
                el.send_keys(value)
                return True
        return False

    if not fill(("学工号", "用户名", "手机号"), ctx.username):
        raise RuntimeError("CAS 表单未找到账号输入框")
    if not fill(("密码",), ctx.password):
        raise RuntimeError("CAS 表单未找到密码输入框")
    time.sleep(0.5)
    for el in driver.find_elements("xpath",
            "//button[contains(., '登录')] | //a[contains(., '登录')] | //input[@type='submit']"):
        try:
            driver.execute_script("arguments[0].click()", el)
            break
        except WebDriverException:
            continue
    time.sleep(6)
    if "cas.bnu.edu.cn" in driver.current_url:
        raise RuntimeError("CAS 登录未跳转,请检查统一认证账号密码")
    cookies = driver.get_cookies()
    if not cookies:
        raise RuntimeError("登录后未获取到 Cookie")
    save_cookies(ctx, cookies)
    ctx.log("登录成功,Cookie 已缓存")
    return cookies


def cookie_file(ctx) -> Path:
    return ctx.datadir / "cookies.json"


def save_cookies(ctx, cookies: list) -> None:
    with open(cookie_file(ctx), "w", encoding="utf-8") as f:
        json.dump({"saved_at": time.time(), "cookies": cookies}, f, ensure_ascii=False)


def load_cookies(ctx) -> list | None:
    p = cookie_file(ctx)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("saved_at", 0) > COOKIE_LIFE:
            ctx.log("缓存 Cookie 已过期")
            return None
        return data.get("cookies") or []
    except (json.JSONDecodeError, OSError):
        return None


def inject_cookies(driver, cookies: list) -> None:
    driver.get(LOGIN_HOME)
    driver.delete_all_cookies()
    for c in cookies:
        try:
            driver.add_cookie(c)
        except Exception:
            pass


def ensure_login(ctx, driver) -> list:
    """有有效 cookie 用 cookie,否则现场登录;返回可用 cookies。"""
    cached = load_cookies(ctx)
    if cached:
        inject_cookies(driver, cached)
        driver.get(LOGIN_HOME)
        time.sleep(4)
        if not driver.find_elements("css selector", ".login-btn"):
            ctx.log("使用缓存 Cookie")
            return cached
        ctx.log("缓存 Cookie 已失效")
    return do_login(ctx, driver)


# ---------------- 课程结构发现 ----------------

def parse_study_url(url: str) -> tuple[str, str] | None:
    m = STUDY_URL_RE.search(url)
    return (m.group(1), m.group(2)) if m else None


def fetch_json(driver, url: str):
    return driver.execute_async_script(FETCH_JSON_JS, url, XTBZ)


def list_leaves(driver, sign: str, cid: str) -> list[dict]:
    """返回课程全部叶子(拍平),带章节名。课程未发布会抛 RuntimeError。"""
    ch = fetch_json(driver, CHAPTER_API.format(cid=cid, sign=sign))
    if not (isinstance(ch, dict) and ch.get("success")):
        msg = ch.get("msg", "未知错误") if isinstance(ch, dict) else "非 JSON 响应"
        raise RuntimeError(f"章节接口失败({msg}),课程可能未发布学习内容")
    out = []
    for chapter in (ch.get("data") or {}).get("course_chapter", []):
        for section in chapter.get("section_leaf_list", []):
            leaves = section.get("leaf_list")
            if not leaves:
                if section.get("leaf_type") is not None:
                    leaves = [section]
                else:
                    continue
            for lf in leaves:
                out.append({
                    "id": lf["id"],
                    "name": lf.get("name", ""),
                    "leaf_type": lf.get("leaf_type"),
                    "chapter": chapter.get("name", ""),
                    "locked": lf.get("is_locked", False),
                    "shown": lf.get("is_show", True),
                })
    return out


def fetch_schedule(driver, sign: str, cid: str) -> dict:
    sc = fetch_json(driver, SCHEDULE_API.format(cid=cid, sign=sign))
    if isinstance(sc, dict):
        return (sc.get("data") or {}).get("leaf_schedules") or {}
    return {}


def is_leaf_done(sch: dict, leaf_id) -> bool:
    """schedule 值有三种形态:1=完成;{total,done}=练习;小数=视频观看进度。

    视频进度为小数时(如 0.0857)不能当真值 —— 仅 1(或 done>=total)算完成;
    视频真实完成状态由播放页完成度元素最终裁决,这里只做初筛。
    """
    s = sch.get(str(leaf_id))
    if s is None:
        return False
    if isinstance(s, dict):
        return s.get("done", 0) >= s.get("total", 1)
    if isinstance(s, (int, float)):
        return s >= 1
    return bool(s)


def collect_targets(ctx, driver, pairs: list[tuple[str, str]], only_unfinished: bool) -> list[dict]:
    """汇总目标视频: [{sign,cid,leaf_id,name,course}]。"""
    targets = []
    for sign, cid in pairs:
        try:
            leaves = list_leaves(driver, sign, cid)
        except RuntimeError as e:
            ctx.status(str(e), course=f"{sign}/{cid}")
            continue
        sch = fetch_schedule(driver, sign, cid) if only_unfinished else {}
        videos = [lf for lf in leaves
                  if lf.get("leaf_type") == LEAF_TYPE_VIDEO
                  and lf.get("shown") and not lf.get("locked")]
        pending = [lf for lf in videos
                   if not (only_unfinished and is_leaf_done(sch, lf["id"]))]
        ctx.status(f"课程 {sign}/{cid}: 视频共 {len(videos)},待刷 {len(pending)}")
        for lf in pending:
            targets.append({
                "sign": sign, "cid": cid, "leaf_id": lf["id"],
                "name": lf["name"], "course": f"{sign}/{cid}",
            })
    return targets


def discover_all_courses(ctx, driver) -> list[tuple[str, str]]:
    """study_urls 为空时,遍历"我的课"页课程卡片收集 (sign, cid)。

    每次点击后浏览器会跳转,元素引用全部失效,因此按索引逐个重查。
    """
    driver.get(COURSELIST_URL)
    time.sleep(6)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    index = 0
    while True:
        cards = driver.find_elements("css selector", "[class*='card']")
        if index >= len(cards):
            break
        card = cards[index]
        index += 1
        text = (card.text or "").strip()
        # 真卡片:两行文本(课名 / 学期-班级),且尺寸可见
        if not text or len(text) > 60 or "\n" not in text:
            continue
        size = card.size
        if size["width"] < 120 or size["height"] < 40:
            continue
        try:
            driver.execute_script("arguments[0].click()", card)
            time.sleep(3)
            m = STUDY_URL_RE.search(driver.current_url)
            if m and (m.group(1), m.group(2)) not in seen:
                seen.add((m.group(1), m.group(2)))
                pairs.append((m.group(1), m.group(2)))
                ctx.log(f"发现课程: {text.splitlines()[0]} → {m.group(1)}/{m.group(2)}")
        except WebDriverException as e:
            ctx.log(f"卡片点击失败,跳过: {str(e).splitlines()[0] if str(e) else e!r}")
        finally:
            driver.get(COURSELIST_URL)
            time.sleep(3)
    ctx.status(f"遍历发现课程 {len(pairs)} 门")
    return pairs


# ---------------- 播放 ----------------

class Tracker:
    def __init__(self, total: int):
        self.total = total
        self.completed = 0
        self.lock = threading.Lock()
        self.workers: dict[int, dict] = {}
        self.results: dict[str, str] = {}

    def set_worker(self, wid: int, **kw):
        with self.lock:
            self.workers.setdefault(wid, {}).update(kw)

    def mark(self, key: str, status: str):
        with self.lock:
            self.results[key] = status
            if status == "已完成":
                self.completed += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "done": self.completed,
                "total": self.total,
                "workers": {str(k): dict(v) for k, v in self.workers.items()},
            }


def _read_total_seconds(driver) -> float:
    """从播放器时间控件读取总时长("00:10:33" → 633 秒)。

    该元素仅在页面刚加载、尚未点击播放时有值,点击后会被清空,必须先读。
    """
    for xp in (TIME_TOTAL_XPATH,):
        try:
            els = driver.find_elements("xpath", xp)
            if els:
                return _parse_time(els[0].text)
        except WebDriverException:
            pass
    # 元素不存在时退回 video.duration
    try:
        d = driver.execute_script(
            "const v=document.querySelector('#video-box video');"
            "return v && isFinite(v.duration) ? v.duration : 0;")
        return float(d or 0)
    except WebDriverException:
        return 0.0


def _parse_time(text: str) -> float:
    """'00:10:33' / '10:33' → 秒;解析失败返回 0。"""
    m = re.search(r"(?:(\d+):)?(\d{1,2}):(\d{2})\s*$", text or "")
    if not m:
        return 0.0
    h, mn, s = m.group(1), m.group(2), m.group(3)
    return int(h or 0) * 3600 + int(mn) * 60 + int(s)


def _read_status_text(driver) -> str:
    """读取完成度元素文本:未完成='完成度:N%',完成='已完成';读不到返回 ''。"""
    for xp in (STATUS_EXACT_XPATH, STATUS_LOOSE_XPATH):
        try:
            els = driver.find_elements("xpath", xp)
        except WebDriverException:
            continue
        for el in els:
            try:
                t = (el.text or "").strip()
            except WebDriverException:
                continue
            if t:
                return t
    return ""


def play_video(driver, sign: str, cid: str, leaf_id, speedup: float,
               on_progress=None) -> str:
    """打开一个视频页并播放到完成。返回终态文本。

    完成判定以页面完成度元素为准:文本变为"已完成"才返回"已完成"
    (未完成时同一元素显示"完成度:N%",N 即服务器真实进度)。
    schedule API 仅作辅助:若页面元素读不到,用其兜底;两者都读不到才继续等。

    on_progress(percent, remain_s, dur_s):周期回调,用于上报进度与预计完成时间。
      percent = 完成度元素里的 N(读不到时用本地播放进度估算)
      remain_s = 剩余视频秒数 / 倍速(本地按视频真实播放秒数计时)
      dur_s = 视频总时长(点击播放前从时间控件读取)
    """
    url = VIDEO_PAGE.format(sign=sign, cid=cid, leaf_id=leaf_id)
    driver.get(url)
    try:
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located(("tag name", "video")))
    except Exception:
        return "页面无播放器"

    # 总时长必须在点击播放前读(点击后时间控件被清空);
    # 元数据未加载时元素可能暂为空,最多重试 8 秒
    dur_s = 0.0
    for _ in range(8):
        dur_s = _read_total_seconds(driver)
        if dur_s > 0:
            break
        time.sleep(1)

    try:
        # 已看完的视频停在末尾,seek 0 以便重刷
        driver.execute_script(
            "const v=document.querySelector('#video-box video');"
            "if (v && v.duration && v.currentTime > v.duration - 1) v.currentTime = 0;")
    except WebDriverException:
        pass

    try:
        btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(("xpath", PLAY_BTN_XPATH)))
        btn.click()  # 原生 click = 可信事件,自动播放策略放行
    except Exception:
        return "找不到播放按钮"

    # 播放器初始化后再注入(此前实测:点击前注入会被播放器重置吞掉)
    driver.execute_script(ANTI_PAUSE_JS)
    driver.execute_script(speed_hijack_js(speedup))
    driver.execute_script(speed_keeper_js(speedup))
    started_at = time.time()
    last_report = 0.0

    deadline = time.time() + 3 * 3600
    poll_api = time.time() + 60   # schedule API 低频轮询
    api_confirmed_at: float | None = None
    while time.time() < deadline:
        # 通道 a: 页面完成度元素(权威依据)
        status_text = _read_status_text(driver)
        if status_text == "已完成":
            if on_progress:
                on_progress(100, 0, dur_s)
            return "已完成"

        # 已播视频秒数:优先读页面 video.currentTime(真实,含卡顿),
        # 读不到时按挂钟 × 倍速估算
        played = (time.time() - started_at) * speedup
        try:
            cur = driver.execute_script(
                "const v=document.querySelector('#video-box video');"
                "return v ? v.currentTime : null;")
            if isinstance(cur, (int, float)):
                played = float(cur)
        except WebDriverException:
            pass
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", status_text)
        percent = float(m.group(1)) if m else (
            min(99.0, played / dur_s * 100) if dur_s > 0 else 0.0)
        remain_s = max(0.0, dur_s - played) / speedup if dur_s > 0 else None
        if on_progress and time.time() - last_report >= 2.0:
            last_report = time.time()
            on_progress(percent, remain_s, dur_s)

        # 通道 b: schedule API 兜底(页面元素被改版破坏时仍能结束)。
        # API 确认完成后再给页面元素 120s 刷新;届时仍未显示"已完成"则按
        # 服务器记录收尾,避免死等
        if api_confirmed_at is None and time.time() >= poll_api:
            poll_api = time.time() + 60
            try:
                sch = fetch_schedule(driver, sign, cid)
                if is_leaf_done(sch, leaf_id):
                    api_confirmed_at = time.time()
            except Exception:
                pass
        if api_confirmed_at is not None and time.time() - api_confirmed_at > 120:
            return "服务器已记录(页面未刷新)"
        time.sleep(5)
    return "等待超时"


def worker(ctx, wid: int, jobs: queue.Queue, cookies: list, tracker: Tracker, speedup: float):
    driver = make_driver(ctx, headless=bool(ctx.cfg("headless", True)))
    local = list(cookies)

    def on_prog(percent, remain_s, dur_s):
        # 播放中周期回调:完成度%(服务器数据)、预计剩余时间、视频总时长
        tracker.set_worker(wid, percent=round(percent, 1),
                           eta_s=round(remain_s) if remain_s is not None else None,
                           total_s=round(dur_s) if dur_s else None)

    try:
        inject_cookies(driver, local)
        while True:
            try:
                job = jobs.get_nowait()
            except queue.Empty:
                tracker.set_worker(wid, status="空闲", title="",
                                   percent=None, eta_s=None, total_s=None)
                return
            key = f"{job['sign']}/{job['cid']}/{job['leaf_id']}"
            tracker.set_worker(wid, status="播放中", title=job["name"],
                               percent=0.0, eta_s=None, total_s=None)
            try:
                status = play_video(driver, job["sign"], job["cid"], job["leaf_id"],
                                    speedup, on_progress=on_prog)
                if status in ("页面无播放器", "找不到播放按钮"):
                    # cookie 失效 → 现场重登后重试一次
                    tracker.set_worker(wid, status="重登中", title=job["name"],
                                       percent=None, eta_s=None, total_s=None)
                    local = do_login(ctx, driver)
                    inject_cookies(driver, local)
                    status = play_video(driver, job["sign"], job["cid"], job["leaf_id"],
                                        speedup, on_progress=on_prog)
                tracker.mark(key, status)
                tracker.set_worker(wid, status=status, title=job["name"],
                                   percent=100.0 if status == "已完成" else None,
                                   eta_s=None, total_s=None)
                ctx.log(f"[w{wid}] {job['name']} → {status}")
            except Exception as e:
                tracker.mark(key, "出错")
                tracker.set_worker(wid, status="出错", title=job["name"],
                                   percent=None, eta_s=None, total_s=None)
                ctx.log(f"[w{wid}] {job['name']} 异常: {e!r}")
                time.sleep(2)
            finally:
                jobs.task_done()
    except Exception as e:
        tracker.set_worker(wid, status="崩溃")
        ctx.log(f"[w{wid}] worker 崩溃: {e!r}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        tracker.set_worker(wid, status="已关闭")


# ---------------- 主流程 ----------------

def main() -> None:
    ctx = PluginContext()
    speedup = float(ctx.cfg("speedup", 3.0))
    workers_n = int(ctx.cfg("workers", 4))
    only_unfinished = bool(ctx.cfg("only_unfinished", True))
    study_urls = [u.strip() for u in (ctx.cfg("study_urls") or []) if u.strip()]

    if not ctx.username or not ctx.password:
        ctx.log("未配置统一认证账号(全局设置或插件配置),退出")
        ctx.progress(done=0, total=0)
        return

    cookies: list = []
    targets: list[dict] = []
    driver = make_driver(ctx, headless=bool(ctx.cfg("headless", True)))
    try:
        cookies = ensure_login(ctx, driver)
        if study_urls:
            pairs = [p for p in (parse_study_url(u) for u in study_urls) if p]
            bad = len(study_urls) - len(pairs)
            if bad:
                ctx.status(f"{bad} 条链接无法解析(需为课程 studycontent 页 URL)")
        else:
            ctx.log("未配置学习内容页链接,遍历全部已选课程")
            pairs = discover_all_courses(ctx, driver)
        targets = collect_targets(ctx, driver, pairs, only_unfinished)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if not targets:
        ctx.log("没有需要刷的视频,任务结束")
        ctx.progress(done=0, total=0)
        return

    ctx.log(f"共 {len(targets)} 个视频待刷,{workers_n} 并发,倍速 {speedup}")
    jobs: queue.Queue = queue.Queue()
    for t in targets:
        jobs.put(t)
    tracker = Tracker(len(targets))

    threads = []
    for i in range(min(workers_n, len(targets))):
        th = threading.Thread(target=worker,
                              args=(ctx, i, jobs, cookies, tracker, speedup),
                              daemon=True)
        th.start()
        threads.append(th)
        time.sleep(1)

    while any(th.is_alive() for th in threads):
        ctx.progress(**tracker.snapshot())
        time.sleep(2)
    ctx.progress(**tracker.snapshot())

    done = sum(1 for v in tracker.results.values() if v == "已完成")
    ctx.log(f"全部任务结束:成功 {done}/{len(targets)}")
    for key, status in tracker.results.items():
        if status != "已完成":
            ctx.log(f"  未完成: {key} ({status})")


if __name__ == "__main__":
    main()
