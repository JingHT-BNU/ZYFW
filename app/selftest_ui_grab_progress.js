/* 配置界面「抓取专业目录」进度条行为自测(离线 DOM 桩, 用 Node 运行):
   1. init 不自动发起抓取(点击触发的回归);
   2. 点击按钮 → 进度条进入启动态 + 恰好一次 POST + 启动轮询;
   3. 旧版主程序(/api/plugins 无 progress 字段)时, 从事件日志兜底解析进度并渲染
      百分比/组合数/预计剩余时间(定位到本次进程启动标记之后, 不吃旧进度);
   4. 新版主程序(带 progress 字段)时直接用原生进度, 不请求事件日志;
      打开页面时任务已在运行 → 接管监控且不重复发起;
   5. 目录完成后收起进度条、恢复按钮、停止轮询。

运行: node app\selftest_ui_grab_progress.js
*/
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "plugins", "course-grab", "ui.html"), "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) throw new Error("ui.html 中未找到脚本");

const tick = () => new Promise((r) => setTimeout(r, 0));

function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

function makeEl(id) {
  return {
    id, hidden: true, disabled: false, value: "", textContent: "",
    className: "", innerHTML: "", title: "", placeholder: "",
    checked: false, type: "", style: {}, handlers: {}, children: [],
    selectedOptions: [],
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    async dispatch(ev) { for (const fn of (this.handlers[ev] || []).slice()) await fn(); },
  };
}

function makeZ(opts) {
  // opts: { majors: fn, plugins: fn, events: fn, courses: fn, postError: 布尔 }
  const calls = { api: [], post: [], save: [] };
  return {
    calls,
    getConfig: async () => ({ mode: "auto", poll_interval: 2, headless: true,
                             username: "", password: "", courses: {} }),
    saveConfig: async (cfg) => { calls.save.push(cfg); return { ok: true }; },
    message() {},
    async api(p) {
      calls.api.push(p);
      if (p === "plugins/course-grab/majors") {
        const v = opts.majors();
        if (v && v.error) throw new Error("404: 专业目录尚未抓取");
        return v;
      }
      if (p.indexOf("plugins/course-grab/major-courses") === 0) {
        const v = opts.courses ? opts.courses() : { error: true };
        if (v && v.error) throw new Error("404: 课程列表尚未缓存");
        return v;
      }
      if (p === "plugins") return { plugins: opts.plugins() };
      if (p.indexOf("plugins/course-grab/logs") === 0) return { events: opts.events() };
      throw new Error("unexpected api: " + p);
    },
    async post(p) {
      calls.post.push(p);
      if (opts.postError) throw new Error(opts.postError);
      return { ok: true };
    },
  };
}

async function boot(Z) {
  const els = {};
  const document = {
    getElementById: (id) => els[id] || (els[id] = makeEl(id)),
    createElement: (tag) => makeEl("<" + tag + ">"),
  };
  let intervalCb = null;
  const sandbox = {
    window: { ZyfwConfigUI: Z },
    document,
    console,
    setInterval(cb) { intervalCb = cb; return 1; },
    clearInterval() { intervalCb = null; },
  };
  sandbox.window.setInterval = sandbox.setInterval;
  sandbox.window.clearInterval = sandbox.clearInterval;
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox, { filename: "ui.html" });
  await tick(); await tick(); await tick();   // 等 init 异步链完成
  return {
    els,
    intervalActive: () => intervalCb !== null,
    fireInterval: async () => {
      const cb = intervalCb;
      if (!cb) throw new Error("监控轮询未启动");
      await cb(); await tick(); await tick();
    },
  };
}

async function main() {
  // ---- 场景 1-3 + 5: 目录缺失 + 旧版主程序(无 progress 字段) ----
  let running = false;
  let catalogState = { error: true };   // 目录尚未抓取
  const Z = makeZ({
    majors: () => catalogState,
    plugins: () => [{ id: "course-grab", running }],   // 旧版: 不带 progress 字段
    events: () => [
      { type: "log", plugin: "course-grab", line: "[00:00:01] 开始抓取" },
      { type: "plugin_status", plugin: "course-grab", status: "running" },
      { type: "log", plugin: "course-grab", line: "[00:00:02] 登录中" },
      { type: "progress", plugin: "course-grab", stage: "catalog",
        done: 60, total: 827, eta_seconds: 120 },
    ],
  });
  const t = await boot(Z);

  // 1. init 不自动发起抓取, 只显示引导横幅
  assert(Z.calls.post.length === 0, "init 不得自动发起抓取");
  assert(t.els["cg-catalog-missing"].hidden === false, "目录缺失应显示引导横幅");

  // 2. 点击 → 启动态进度条 + 恰好一次 POST + 启动轮询
  running = true;
  await t.els["cg-grab-catalog"].dispatch("click");
  assert(Z.calls.post.length === 1
         && Z.calls.post[0] === "plugins/course-grab/majors/refresh",
         "点击应恰好 POST 一次 refresh: " + JSON.stringify(Z.calls.post));
  assert(t.els["cg-catalog-progress"].hidden === false, "点击后应显示进度条");
  assert(t.els["cg-catalog-progress-text"].textContent === "正在启动抓取…",
         t.els["cg-catalog-progress-text"].textContent);
  assert(t.intervalActive(), "点击后应启动监控轮询");

  // 3. 轮询: /api/plugins 无 progress → 从事件日志兜底解析并渲染
  await t.fireInterval();
  assert(Z.calls.api.some((p) => p.indexOf("plugins/course-grab/logs") === 0),
         "旧版主程序应回退到事件日志解析进度");
  assert(t.els["cg-catalog-bar"].style.width === "7%",
         "bar width = " + t.els["cg-catalog-bar"].style.width);   // round(60/827*100)=7
  const txt = t.els["cg-catalog-progress-text"].textContent;
  assert(txt.indexOf("已完成 60/827 组合(7%)") >= 0, txt);
  assert(txt.indexOf("预计剩余 2 分 0 秒") >= 0, txt);
  console.log("[ok] fallback renders progress from event log (old host)");

  // 5. 目录完成(partial=false)且进程已结束 → 收起进度条/恢复按钮/停止轮询
  catalogState = { partial: false, updated_at: "2026-10-25 12:00:00", levels: [] };
  running = false;
  await t.fireInterval();
  assert(t.els["cg-catalog-progress"].hidden === true, "完成后应收起进度条");
  assert(t.els["cg-grab-catalog"].disabled === false, "完成后应恢复按钮");
  assert(!t.intervalActive(), "完成后应停止轮询");
  console.log("[ok] completion hides bar, re-enables button, stops polling");

  // ---- 场景 4a: 新版主程序(带 progress 字段), 打开页面时任务已在运行 ----
  // 接管监控走原生 progress, 不请求事件日志, 不重复发起抓取
  const Z2 = makeZ({
    majors: () => ({ error: true }),
    plugins: () => [{ id: "course-grab", running: true,
                      progress: { stage: "catalog", done: 100, total: 827, eta_seconds: 30 } }],
    events: () => { throw new Error("不应请求事件日志"); },
  });
  const t2 = await boot(Z2);
  assert(Z2.calls.post.length === 0, "接管监控不得重复发起抓取");
  await t2.fireInterval();   // init 已接管, 直接触发一次轮询
  assert(t2.els["cg-catalog-bar"].style.width === "12%",
         "bar width = " + t2.els["cg-catalog-bar"].style.width);  // round(100/827*100)=12
  assert(!Z2.calls.api.some((p) => p.indexOf("/logs") >= 0), "不应请求事件日志");
  console.log("[ok] native progress field renders without log fallback");

  // ---- 场景 4b: 监控中任务变成其他类型(progress.stage != catalog) → 解除监控 ----
  // (打开页面时进程已被其他任务占用则 init 根本不会接管, 轮询无从启动;
  //  这里构造"接管时是目录任务、轮询时变成课程抓取"的时序来覆盖解除分支)
  let z4progress = { stage: "catalog", done: 10, total: 827 };
  const Z3 = makeZ({
    majors: () => ({ error: true }),
    plugins: () => [{ id: "course-grab", running: true, progress: z4progress }],
    events: () => [],
  });
  const t3 = await boot(Z3);
  assert(t3.intervalActive(), "init 应接管目录抓取监控");
  z4progress = { stage: "courses", done: 1, total: 3 };   // 进程被其他任务占用
  await t3.fireInterval();
  assert(!t3.intervalActive(), "非目录任务应解除监控");
  assert(t3.els["cg-grab-catalog"].disabled === false, "解除监控后应恢复按钮");
  console.log("[ok] non-catalog task detaches watch and re-enables button");

  // ---- 场景 6: 空配置可保存 + 运行模式仅两个 + 课程抓取按钮直发任务 ----
  const CATALOG6 = { partial: false, updated_at: "2026-10-25 09:00:00", levels: [
    { v: "1", t: "本科", grades: [
      { v: "2025", t: "2025", depts: [
        { v: "001", t: "学院A", majors: [{ v: "080717", t: "人工智能" }] },
      ]},
    ]},
  ]};
  let courses6 = { error: true };   // 课程尚未缓存
  const Z6 = makeZ({
    majors: () => CATALOG6,
    plugins: () => [{ id: "course-grab", running: false }],
    events: () => [],
    courses: () => courses6,
  });
  const t6 = await boot(Z6);
  // 运行模式只剩两个: 轮询选课 + 单次选课
  // (桩 DOM 不解析静态 <option>, 直接断言下发的 HTML 源文本)
  assert(/<option value="auto">/.test(html) && /<option value="once">/.test(html)
         && !/<option value="fetch_major">/.test(html), "运行模式应只剩 auto/once 两项");
  assert(/id="cg-load"[^>]*>开始专业课程抓取</.test(html),
         "选择专业后的按钮应为「开始专业课程抓取」");
  // 空配置可直接保存(不再要求先勾选课程)
  await t6.els["cg-save"].dispatch("click");
  assert(Z6.calls.save.length === 1, "空配置应能直接保存");
  assert(Object.keys(Z6.calls.save[0].courses).length === 0, "保存的课程列表应为空");
  // 逐级选满四级 → 自动尝试加载缓存(未缓存则提示) → 启用抓取按钮
  t6.els["cg-level"].value = "1";
  await t6.els["cg-level"].dispatch("change");
  t6.els["cg-grade"].value = "2025";
  await t6.els["cg-grade"].dispatch("change");
  t6.els["cg-dept"].value = "001";
  await t6.els["cg-dept"].dispatch("change");
  t6.els["cg-major"].value = "080717";
  await t6.els["cg-major"].dispatch("change");
  await tick();   // maybeAutoLoad 异步加载课程缓存
  assert(t6.els["cg-load"].disabled === false, "选满四级后应启用抓取按钮");
  assert(t6.els["cg-courses-empty"].hidden === false, "未缓存专业应提示先抓取");
  assert(Z6.calls.post.length === 0, "加载缓存不得发起抓取任务");
  // 点击「开始专业课程抓取」→ 恰好一次 POST + 显示进度条 + 按钮禁用 + 启动轮询
  await t6.els["cg-load"].dispatch("click");
  const coursePosts = Z6.calls.post.filter(
    (p) => p === "plugins/course-grab/major-courses/refresh");
  assert(coursePosts.length === 1,
         "课程抓取应恰好 POST 一次: " + JSON.stringify(Z6.calls.post));
  assert(t6.els["cg-courses-progress"].hidden === false, "课程抓取应显示进度条");
  assert(t6.els["cg-load"].disabled === true, "抓取中按钮应禁用");
  assert(t6.intervalActive(), "课程抓取应启动监控轮询");
  console.log("[ok] empty config saves; course grab button posts refresh + progress");

  console.log("ALL UI GRAB PROGRESS TESTS PASSED");
}

main().catch((e) => { console.error(e); process.exit(1); });
