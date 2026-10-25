/* ZYFW 工具集前端 — Vue 3 + Element Plus (本地 vendor) */
(function () {
  "use strict";

  const { createApp, reactive, ref, computed, watch, nextTick, onMounted } = Vue;
  const El = ElementPlus;
  const Icons = ElementPlusIconsVue;

  // ---------------- API 工具 ----------------

  async function api(path, options) {
    const res = await fetch("/api" + path, options);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const data = await res.json();
        if (data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      } catch (e) { /* ignore */ }
      throw new Error(detail);
    }
    return res.json();
  }

  function post(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  // ---------------- 状态 ----------------

  const state = reactive({
    page: "dashboard",            // dashboard | plugins | settings | detail
    detailId: null,
    plugins: [],
    globalConfig: null,
    wsConnected: false,
    themeMode: localStorage.getItem("zyfw-theme") || "system",
    logs: {},                     // pluginId -> [ {line, cls} ]
    progress: {},                 // pluginId -> {done,total,workers,...}
    detailConfig: null,
    detailSchema: [],
    savingConfig: false,
    savingGlobal: false,
    installing: false,
    uiHtml: null,                 // 自定义配置界面 HTML(插件 config_ui 提供)
  });

  const MAX_LOG_LINES = 2000;

  function pushLog(pluginId, line, cls) {
    if (!state.logs[pluginId]) state.logs[pluginId] = [];
    const arr = state.logs[pluginId];
    arr.push({ line, cls: cls || "" });
    if (arr.length > MAX_LOG_LINES) arr.splice(0, arr.length - MAX_LOG_LINES);
  }

  // ---------------- WebSocket ----------------

  let ws = null;
  let wsTimer = null;

  function connectWS() {
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    ws = new WebSocket(proto + location.host + "/api/ws");
    ws.onopen = () => { state.wsConnected = true; };
    ws.onclose = () => {
      state.wsConnected = false;
      clearTimeout(wsTimer);
      wsTimer = setTimeout(connectWS, 2500);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "hello") {
        (msg.history || []).forEach(handleEvent);
      } else {
        handleEvent(msg);
      }
    };
  }

  function handleEvent(msg) {
    const pid = msg.plugin;
    if (!pid) return;
    if (msg.type === "log") {
      let cls = "";
      if (/error|异常|崩溃|失败|Traceback/i.test(msg.line)) cls = "status";
      pushLog(pid, msg.line, cls);
    } else if (msg.type === "progress") {
      const fields = Object.assign({}, msg);
      delete fields.type; delete fields.plugin; delete fields.__event__;
      state.progress[pid] = fields;
      pushLog(pid, "[进度] " + JSON.stringify(fields), "progress");
    } else if (msg.type === "status") {
      pushLog(pid, "◆ " + (msg.text || ""), "status");
    } else if (msg.type === "plugin_status") {
      updatePluginStatus(pid, msg.status);
      if (msg.status === "stopped" || msg.status === "crashed" || msg.status === "exited") {
        // 终态后不再保留上一轮进度, 否则仪表盘会永久显示旧进度条。
        delete state.progress[pid];
        pushLog(pid, "—— 进程结束 (" + msg.status + (msg.exit_code !== undefined && msg.exit_code !== null ? ", exit " + msg.exit_code : "") + ") ——", "sysinfo");
      }
    } else if (msg.type === "plugin_installed" || msg.type === "plugin_uninstalled") {
      loadPlugins();
    }
  }

  function updatePluginStatus(pid, status) {
    const p = state.plugins.find((x) => x.id === pid);
    if (p) {
      p.status = status;
      p.running = status === "running" || status === "starting" || status === "stopping";
    }
  }

  // ---------------- 主题 ----------------

  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function applyTheme() {
    const mode = state.themeMode;
    const dark = mode === "dark" || (mode === "system" && media.matches);
    document.documentElement.classList.toggle("dark", dark);
  }

  media.addEventListener("change", () => { if (state.themeMode === "system") applyTheme(); });

  function setThemeMode(mode) {
    state.themeMode = mode;
    localStorage.setItem("zyfw-theme", mode);
    applyTheme();
  }

  // ---------------- 数据加载 ----------------

  async function loadPlugins() {
    const data = await api("/plugins");
    state.plugins = data.plugins;
    for (const p of state.plugins) {
      if (p.progress) state.progress[p.id] = p.progress;
      else if (!p.running) delete state.progress[p.id];
    }
  }

  async function loadGlobal() {
    const data = await api("/global");
    state.globalConfig = data.config;
  }

  let detailRequest = 0;

  async function openDetail(id) {
    const p = state.plugins.find((x) => x.id === id);
    if (!p) return;
    const requestId = ++detailRequest;
    state.detailId = id;
    state.page = "detail";
    state.detailSchema = p.config_schema || [];
    state.detailConfig = null;
    state.uiHtml = null;
    const data = await api("/plugins/" + id + "/config");
    if (requestId !== detailRequest || state.page !== "detail") return;
    if (p.config_ui) {
      // 自定义配置界面插件: 配置对象原样交给界面脚本, 不做编辑模型加工
      state.detailConfig = data.config;
      try {
        const ui = await api("/plugins/" + id + "/ui");
        if (requestId !== detailRequest || state.page !== "detail") return;
        state.uiHtml = ui.html;
      } catch (e) {
        // 界面加载失败时回退到 schema 简易表单, 需要编辑模型
        state.uiHtml = null;
        state.detailConfig = buildEditorModel(state.detailSchema, data.config);
        El.ElMessage.warning("自定义配置界面加载失败, 已回退简易表单: " + e.message);
      }
    } else {
      state.detailConfig = buildEditorModel(state.detailSchema, data.config);
    }
    if (!state.logs[id]) {
      const logs = await api("/plugins/" + id + "/logs?limit=300");
      if (requestId !== detailRequest) return;
      state.logs[id] = logs.events
        .filter((e) => e.type === "log")
        .map((e) => ({ line: e.line, cls: "" }));
    }
  }

  // ---------------- 配置表单辅助 ----------------

  function coursesToRows(courses) {
    return Object.entries(courses || {}).map(([code, classes]) => ({
      code,
      classes: (classes || []).join(", "),
    }));
  }

  function rowsToCourses(rows) {
    const out = {};
    (rows || []).forEach((r) => {
      const code = (r.code || "").trim();
      if (!code) return;
      const classes = (r.classes || "").split(/[,，\s]+/).filter(Boolean);
      out[code] = classes;
    });
    return out;
  }

  function buildEditorModel(schema, config) {
    // 为 course-table / string-list 生成便于编辑的中间结构
    const model = JSON.parse(JSON.stringify(config || {}));
    schema.forEach((f) => {
      if (f.type === "course-table") {
        model.__rows__ = model.__rows__ || coursesToRows(model[f.key]);
      }
      if (f.type === "string-list") {
        model.__text__ = model.__text__ || {};
        model.__text__[f.key] = (model[f.key] || []).join("\n");
      }
    });
    return model;
  }

  function serializeEditorModel(schema, model) {
    const out = JSON.parse(JSON.stringify(model));
    schema.forEach((f) => {
      if (f.type === "course-table") {
        out[f.key] = rowsToCourses(out.__rows__ || []);
      }
      if (f.type === "string-list") {
        const text = (out.__text__ && out.__text__[f.key]) || "";
        out[f.key] = text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      }
    });
    delete out.__rows__;
    delete out.__text__;
    return out;
  }

  // ---------------- Vue 应用 ----------------

  const app = createApp({
    setup() {
      const logBoxRef = ref(null);

      const detailPlugin = computed(() =>
        state.plugins.find((x) => x.id === state.detailId) || null);

      const runningCount = computed(() => state.plugins.filter((p) => p.running).length);

      const detailModel = computed({
        get: () => state.detailConfig,
        set: (v) => { state.detailConfig = v; },
      });

      onMounted(async () => {
        applyTheme();
        connectWS();
        await loadPlugins();
        await loadGlobal();
      });

      // 日志自动滚动
      watch(
        () => {
          const id = state.detailId;
          const arr = state.logs[id];
          return arr ? arr.length : 0;
        },
        async () => {
          await nextTick();
          const box = logBoxRef.value;
          if (!box) return;
          const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
          if (nearBottom) box.scrollTop = box.scrollHeight;
        }
      );

      const statusText = (p) => ({
        running: "运行中", stopped: "已停止", exited: "已结束",
        crashed: "异常退出", stopping: "停止中…", starting: "启动中…",
      }[p.status] || "已停止");

      const statusTagType = (p) =>
        p.running ? "success" : (p.status === "crashed" ? "danger" : "info");

      async function refresh() {
        await loadPlugins();
      }

      async function startPlugin(id) {
        try {
          await post("/plugins/" + id + "/start");
          El.ElMessage.success("已启动");
          await refresh();
        } catch (e) { El.ElMessage.error(e.message); }
      }

      async function stopPlugin(id) {
        try {
          await post("/plugins/" + id + "/stop");
          El.ElMessage.info("停止指令已发送");
          await refresh();
        } catch (e) { El.ElMessage.error(e.message); }
      }

      async function saveDetailConfig() {
        if (!state.detailConfig) return;
        state.savingConfig = true;
        try {
          const config = serializeEditorModel(state.detailSchema, state.detailConfig);
          await api("/plugins/" + state.detailId + "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config }),
          });
          El.ElMessage.success("配置已保存");
        } catch (e) { El.ElMessage.error(e.message); }
        finally { state.savingConfig = false; }
      }

      async function saveGlobal() {
        state.savingGlobal = true;
        try {
          await api("/global", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config: state.globalConfig }),
          });
          El.ElMessage.success("全局配置已保存");
        } catch (e) { El.ElMessage.error(e.message); }
        finally { state.savingGlobal = false; }
      }

      async function uninstallPlugin(p) {
        try {
          await El.ElMessageBox.confirm(
            "确定删除插件「" + p.name + "」?其配置数据将保留。",
            "删除确认", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
          );
        } catch (e) { return; }
        try {
          await api("/plugins/" + p.id, { method: "DELETE" });
          El.ElMessage.success("已删除");
          await refresh();
        } catch (e) { El.ElMessage.error(e.message); }
      }

      function triggerInstall() {
        const input = document.getElementById("zip-input");
        input.value = "";
        input.click();
      }

      async function onZipChosen(ev) {
        const file = ev.target.files && ev.target.files[0];
        if (!file) return;
        state.installing = true;
        try {
          const fd = new FormData();
          fd.append("file", file);
          const res = await api("/plugins/install", { method: "POST", body: fd });
          El.ElMessage.success("插件安装成功: " + res.name);
          await refresh();
        } catch (e) { El.ElMessage.error(e.message); }
        finally { state.installing = false; }
      }

      function progressInfo(pid) {
        return state.progress[pid] || null;
      }

      function workerList(pid) {
        const info = state.progress[pid];
        if (!info || !info.workers) return [];
        return Object.entries(info.workers).map(([id, w]) => ({
          id,
          status: w.status || "-",
          title: w.title || w.url || "",
          percent: typeof w.percent === "number" ? w.percent : null,
          etaS: (w.eta_s === null || w.eta_s === undefined) ? null : w.eta_s,
          totalS: (w.total_s === null || w.total_s === undefined) ? null : w.total_s,
        }));
      }

      function workerTagType(s) {
        if (s === "已完成") return "success";
        if (s === "出错" || s === "崩溃") return "danger";
        if (s === "空闲" || s === "已关闭") return "info";
        if (s === "播放中") return "";
        return "warning";
      }

      function fmtDur(s) {
        if (s === null || s === undefined || !isFinite(s)) return "-";
        s = Math.max(0, Math.round(s));
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
        const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
        return h ? h + ":" + mm + ":" + ss : m + ":" + ss;
      }

      function fmtEta(s) {
        if (s === null || s === undefined || !isFinite(s)) return "-";
        s = Math.round(s);
        if (s < 60) return s + " 秒";
        const m = Math.round(s / 60);
        if (m < 60) return m + " 分";
        return Math.floor(m / 60) + " 时 " + (m % 60) + " 分";
      }

      function fmtEtaClock(s) {
        if (s === null || s === undefined || !isFinite(s)) return "-";
        const d = new Date(Date.now() + s * 1000);
        return String(d.getHours()).padStart(2, "0") + ":" +
               String(d.getMinutes()).padStart(2, "0");
      }

      function addCourseRow() {
        if (!state.detailConfig) return;
        if (!state.detailConfig.__rows__) state.detailConfig.__rows__ = [];
        state.detailConfig.__rows__.push({ code: "", classes: "" });
      }

      function removeCourseRow(idx) {
        if (!state.detailConfig || !state.detailConfig.__rows__) return;
        state.detailConfig.__rows__.splice(idx, 1);
      }

      function clearLog(pid) {
        state.logs[pid] = [];
      }

      // ---------------- 自定义配置界面桥(config_ui 插件) ----------------
      // 宿主向界面脚本注入 window.ZyfwConfigUI:
      //   getConfig()  -> Promise<config 对象(密码为 ****** 打码)>
      //   saveConfig(obj) -> Promise 保存(****** 字段由后端保持原值)
      //   api(path, options) -> fetch("/api/"+path) 并按 content-type 解析
      //   post(path, body) -> 便捷 POST JSON
      //   message(type, text) -> 顶部消息(type: success/warning/error/info)
      // 界面 HTML 由 /api/plugins/<id>/ui 下发(已剥离 <style>, 风格用宿主 zy-* 类)。

      function mountCustomUI(el) {
        if (!el) return;
        const pid = state.detailId || "";
        // 先注入桥再重建脚本节点: 界面脚本执行时 window.ZyfwConfigUI 必须已存在
        window.ZyfwConfigUI = {
          getConfig: async () => {
            const data = await api("/plugins/" + pid + "/config");
            state.detailConfig = data.config;
            return data.config;
          },
          saveConfig: async (cfg) => {
            await api("/plugins/" + pid + "/config", {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ config: cfg }),
            });
            state.detailConfig = cfg;
          },
          api: async (path, options) => {
            const res = await fetch("/api/" + String(path).replace(/^\//, ""), options);
            if (!res.ok) {
              let detail = res.statusText;
              try {
                const d = await res.json();
                if (d.detail) detail = typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail);
              } catch (e) { /* ignore */ }
              throw new Error(detail);
            }
            const ct = res.headers.get("content-type") || "";
            return ct.includes("application/json") ? res.json() : res.text();
          },
          post: (path, body) => window.ZyfwConfigUI.api(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: body === undefined ? undefined : JSON.stringify(body),
          }),
          message: (type, text) => {
            const m = El.ElMessage;
            (m[type] || m.info).call(m, text);
          },
        };
        el.innerHTML = state.uiHtml || "";
        // innerHTML 注入的 <script> 不执行, 需重建节点
        el.querySelectorAll("script").forEach((old) => {
          const s = document.createElement("script");
          s.textContent = old.textContent;
          old.replaceWith(s);
        });
      }

      watch(() => state.uiHtml, () => {
        if (state.page === "detail" && state.detailId) {
          nextTick(() => mountCustomUI(document.getElementById("plugin-ui")));
        }
      });

      watch(() => state.detailId, () => {
        state.uiHtml = null;
      });

      return {
        state, logBoxRef, detailPlugin, runningCount, detailModel,
        statusText, statusTagType, progressInfo, workerList,
        workerTagType, fmtDur, fmtEta, fmtEtaClock,
        startPlugin, stopPlugin, saveDetailConfig, saveGlobal,
        uninstallPlugin, triggerInstall, onZipChosen,
        addCourseRow, removeCourseRow, clearLog, setThemeMode, refresh,
        Icons,
      };
    },

    template: `
<div class="layout">
  <input type="file" id="zip-input" accept=".zip" style="display:none" @change="onZipChosen">

  <!-- 侧边栏 -->
  <aside class="sidebar">
    <div class="brand">
      <div class="logo">Z</div>
      <div>ZYFW 工具集<small>大学生活自动化</small></div>
    </div>
    <nav class="nav">
      <div class="nav-item" :class="{active: state.page==='dashboard'}" @click="state.page='dashboard'">
        <el-icon><odometer /></el-icon> 仪表盘
      </div>
      <div class="nav-item" :class="{active: state.page==='plugins'}" @click="state.page='plugins'">
        <el-icon><box /></el-icon> 插件管理
      </div>
      <div class="nav-item" :class="{active: state.page==='settings'}" @click="state.page='settings'">
        <el-icon><setting /></el-icon> 全局设置
      </div>
    </nav>
    <div class="sidebar-foot">
      <span>{{ runningCount }} 个运行中</span>
      <el-radio-group :model-value="state.themeMode" size="small" @update:model-value="setThemeMode">
        <el-radio-button value="light">浅</el-radio-button>
        <el-radio-button value="system">随</el-radio-button>
        <el-radio-button value="dark">深</el-radio-button>
      </el-radio-group>
    </div>
  </aside>

  <!-- 主内容 -->
  <main class="content">

    <!-- 仪表盘 -->
    <template v-if="state.page==='dashboard'">
      <div class="page-header">
        <h1>仪表盘</h1>
        <p>已安装 {{ state.plugins.length }} 个插件</p>
      </div>
      <div class="card-grid">
        <div class="plugin-card" v-for="p in state.plugins" :key="p.id">
          <div class="head">
            <div class="title">
              <div class="icon" style="background:linear-gradient(135deg,var(--zyfw-accent),var(--zyfw-accent-2))">
                <el-icon><magic-stick /></el-icon>
              </div>
              <span>{{ p.name }}</span>
              <el-tag v-if="p.builtin" size="small" type="warning" effect="plain">内置</el-tag>
            </div>
            <el-tooltip :content="statusText(p)" placement="top">
              <span class="running-dot" v-if="p.running"></span>
              <span class="running-dot" style="background:var(--zyfw-border);animation:none" v-else></span>
            </el-tooltip>
          </div>
          <div class="desc">{{ p.description }}</div>
          <div class="progress-row" v-if="progressInfo(p.id)">
            <el-progress
              v-if="progressInfo(p.id).total > 0"
              :percentage="Math.min(100, Math.round((progressInfo(p.id).done||0) / progressInfo(p.id).total * 100))"
              :stroke-width="8" />
            <div class="meta-line" v-else>
              <span v-for="(v,k) in progressInfo(p.id)" :key="k">{{ k }}: {{ v }}</span>
            </div>
          </div>
          <div class="meta-line">
            <span>v{{ p.version }}</span>
            <span>{{ statusText(p) }}</span>
          </div>
          <div class="actions">
            <el-button v-if="!p.running" type="primary" @click="startPlugin(p.id)">
              <el-icon style="margin-right:4px"><video-play /></el-icon>启动
            </el-button>
            <el-button v-else type="danger" plain @click="stopPlugin(p.id)">
              <el-icon style="margin-right:4px"><video-pause /></el-icon>停止
            </el-button>
            <el-button @click="openDetailById(p.id)">配置 / 日志</el-button>
          </div>
        </div>
      </div>
    </template>

    <!-- 插件管理 -->
    <template v-if="state.page==='plugins'">
      <div class="page-header">
        <h1>插件管理</h1>
        <p>安装 zip 插件包,或管理已安装插件</p>
      </div>
      <div class="panel">
        <h2><el-icon><upload /></el-icon> 安装插件</h2>
        <el-button type="primary" :loading="state.installing" @click="triggerInstall">
          <el-icon style="margin-right:4px"><upload /></el-icon>上传插件 zip 包
        </el-button>
        <span class="form-hint" style="margin-left:12px">
          zip 内需包含 plugin.json 与入口 py 文件(可含一层目录)
        </span>
      </div>
      <div class="panel">
        <h2><el-icon><box /></el-icon> 已安装</h2>
        <el-table :data="state.plugins" style="width:100%">
          <el-table-column prop="name" label="名称" min-width="140">
            <template #default="scope">
              {{ scope.row.name }}
              <el-tag v-if="scope.row.builtin" size="small" type="warning" effect="plain">内置</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="id" label="ID" min-width="120" />
          <el-table-column prop="version" label="版本" width="80" />
          <el-table-column label="状态" width="110">
            <template #default="scope">
              <el-tag :type="statusTagType(scope.row)" size="small">{{ statusText(scope.row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="260">
            <template #default="scope">
              <el-button v-if="!scope.row.running" size="small" type="primary" @click="startPlugin(scope.row.id)">启动</el-button>
              <el-button v-else size="small" type="danger" plain @click="stopPlugin(scope.row.id)">停止</el-button>
              <el-button size="small" @click="openDetailById(scope.row.id)">详情</el-button>
              <el-button size="small" type="danger" plain :disabled="scope.row.builtin" @click="uninstallPlugin(scope.row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </template>

    <!-- 全局设置 -->
    <template v-if="state.page==='settings' && state.globalConfig">
      <div class="page-header">
        <h1>全局设置</h1>
        <p>北师大统一身份认证账号(教务系统 / SPOC 通用);凭据用 Windows DPAPI 加密存储,某平台密码不同可在对应插件配置中单独覆盖</p>
      </div>
      <div class="panel">
        <h2><el-icon><user /></el-icon> 统一身份认证账号</h2>
        <el-form label-width="90px" style="max-width:560px">
          <el-form-item label="学号">
            <el-input v-model="state.globalConfig.account.username" placeholder="如 202511101008" />
          </el-form-item>
          <el-form-item label="密码">
            <el-input v-model="state.globalConfig.account.password" type="password" show-password placeholder="已加密存储" />
          </el-form-item>
        </el-form>
      </div>
      <div class="panel">
        <h2><el-icon><tools /></el-icon> 通用参数</h2>
        <el-form label-width="140px" style="max-width:560px">
          <el-form-item label="EdgeDriver 路径">
            <el-input v-model="state.globalConfig.general.driver_path" placeholder="留空 = 自动匹配 (Selenium Manager)" />
          </el-form-item>
          <el-form-item label="默认无头模式">
            <el-switch v-model="state.globalConfig.general.headless_default" />
          </el-form-item>
          <el-form-item label="SPOC 默认并发">
            <el-input-number v-model="state.globalConfig.general.spoc_workers" :min="1" :max="32" />
          </el-form-item>
          <el-form-item label="默认播放倍速">
            <el-input-number v-model="state.globalConfig.general.default_speedup" :min="1" :max="16" :step="0.5" />
          </el-form-item>
        </el-form>
      </div>
      <el-button type="primary" size="large" :loading="state.savingGlobal" @click="saveGlobal">保存全部设置</el-button>
    </template>

    <!-- 插件详情 -->
    <template v-if="state.page==='detail' && detailPlugin">
      <div class="page-header" style="display:flex;align-items:center;gap:14px">
        <el-button circle @click="state.page='dashboard'"><el-icon><back /></el-icon></el-button>
        <div>
          <h1>{{ detailPlugin.name }} <el-tag size="small" effect="plain">v{{ detailPlugin.version }}</el-tag></h1>
          <p>{{ detailPlugin.description }}</p>
        </div>
        <div style="margin-left:auto;display:flex;gap:8px">
          <el-button v-if="!detailPlugin.running" type="primary" @click="startPlugin(detailPlugin.id)">
            <el-icon style="margin-right:4px"><video-play /></el-icon>启动
          </el-button>
          <el-button v-else type="danger" plain @click="stopPlugin(detailPlugin.id)">
            <el-icon style="margin-right:4px"><video-pause /></el-icon>停止
          </el-button>
        </div>
      </div>

      <div class="panel">
        <h2><el-icon><setting /></el-icon> 插件配置</h2>
        <!-- 自定义配置界面(插件 config_ui 提供; 不含 CSS, 风格由宿主 zy-* 类统一) -->
        <div v-if="state.uiHtml" id="plugin-ui"></div>
        <template v-else-if="state.detailConfig">
          <el-form label-width="190px" style="max-width:720px">
            <template v-for="f in state.detailSchema" :key="f.key">
              <!-- 对象型字段(schema 简易表单无对应控件, 仅提示) -->
              <el-form-item v-if="f.type==='specialty'" :label="f.label">
                <span class="form-hint">{{ f.hint || "请在插件自定义配置界面中选择" }}</span>
              </el-form-item>

              <!-- 课程表格 -->
              <el-form-item v-else-if="f.type==='course-table'" :label="f.label">
                <div style="width:100%">
                  <div class="form-hint" style="margin-bottom:8px" v-if="f.hint">{{ f.hint }}</div>
                  <div class="course-row" v-for="(row, idx) in (state.detailConfig.__rows__ || [])" :key="idx">
                    <el-input class="course-code" v-model="row.code" placeholder="课程代码" />
                    <el-input class="course-classes" v-model="row.classes" placeholder="候选班级号,逗号分隔(如 09, 10, 11)" />
                    <el-button type="danger" plain circle size="small" @click="removeCourseRow(idx)">
                      <el-icon><delete /></el-icon>
                    </el-button>
                  </div>
                  <el-button size="small" @click="addCourseRow"><el-icon><plus /></el-icon>添加课程</el-button>
                </div>
              </el-form-item>

              <!-- 字符串列表 -->
              <el-form-item v-else-if="f.type==='string-list'" :label="f.label">
                <div style="width:100%">
                  <el-input
                    v-model="state.detailConfig.__text__[f.key]"
                    type="textarea" :rows="8"
                    placeholder="一行一个链接" />
                  <div class="form-hint" style="margin-top:4px" v-if="f.hint">{{ f.hint }}</div>
                </div>
              </el-form-item>

              <!-- 下拉选择 -->
              <el-form-item v-else-if="f.type==='choice'" :label="f.label">
                <el-select v-model="state.detailConfig[f.key]" style="width:100%">
                  <el-option
                    v-for="(c, i) in (f.choices || [])" :key="c"
                    :value="c"
                    :label="(f.choice_labels && f.choice_labels[i]) || c" />
                </el-select>
              </el-form-item>

              <!-- 数字 -->
              <el-form-item v-else-if="f.type==='number'" :label="f.label">
                <el-input-number
                  v-model="state.detailConfig[f.key]"
                  :min="f.min !== undefined ? f.min : -Infinity"
                  :max="f.max !== undefined ? f.max : Infinity"
                  :step="f.step || 1" />
              </el-form-item>

              <!-- 开关 -->
              <el-form-item v-else-if="f.type==='bool'" :label="f.label">
                <el-switch v-model="state.detailConfig[f.key]" />
              </el-form-item>

              <!-- 密码 / 字符串 -->
              <el-form-item v-else :label="f.label">
                <el-input
                  v-model="state.detailConfig[f.key]"
                  :type="f.type==='password' ? 'password' : 'text'"
                  :show-password="f.type==='password'"
                  :placeholder="f.type==='password' ? '已加密存储' : ''" />
              </el-form-item>
            </template>
          </el-form>
          <el-button type="primary" :loading="state.savingConfig" @click="saveDetailConfig">保存配置</el-button>
          <span class="form-hint" style="margin-left:10px">账号留空时使用全局设置;保存后重启插件生效</span>
        </template>
        <div v-else class="empty-state"><el-icon class="is-loading"><loading /></el-icon> 配置加载中…</div>
      </div>

      <div class="panel">
        <h2>
          <el-icon><document /></el-icon> 运行日志
          <div style="margin-left:auto;display:flex;gap:8px">
            <el-button size="small" @click="clearLog(detailPlugin.id)">清空显示</el-button>
          </div>
        </h2>
        <div class="log-box" ref="logBoxRef">
          <div v-if="!(state.logs[detailPlugin.id] && state.logs[detailPlugin.id].length)" class="empty-state">
            暂无日志,启动插件后这里将实时输出
          </div>
          <div v-for="(l, i) in (state.logs[detailPlugin.id] || [])" :key="i" class="log-line" :class="l.cls">{{ l.line }}</div>
        </div>
        <!-- 每线程工作面板 -->
        <div class="worker-panels" v-if="workerList(detailPlugin.id).length">
          <div class="worker-panel" v-for="w in workerList(detailPlugin.id)" :key="w.id">
            <div class="wp-head">
              <el-tag size="small" :type="workerTagType(w.status)">线程 #{{ w.id }}</el-tag>
              <span class="wp-status">{{ w.status }}</span>
              <span class="wp-title" :title="w.title">{{ w.title || "—" }}</span>
            </div>
            <template v-if="w.percent !== null">
              <el-progress
                :percentage="Math.min(100, Math.round(w.percent * 10) / 10)"
                :stroke-width="8"
                :status="w.status === '已完成' ? 'success' : undefined" />
              <div class="wp-meta">
                <span>进度 {{ w.percent }}%(服务器)</span>
                <span>时长 {{ fmtDur(w.totalS) }}</span>
                <span>剩余 {{ fmtEta(w.etaS) }}</span>
                <span>预计 {{ fmtEtaClock(w.etaS) }} 完成</span>
              </div>
            </template>
          </div>
        </div>
      </div>
    </template>

  </main>
</div>
`,
    methods: {
      openDetailById(id) { openDetail(id); },
    },
  });

  app.use(El, { locale: ElementPlusLocaleZhCn });
  for (const [name, comp] of Object.entries(Icons)) {
    app.component(name, comp);
  }
  app.mount("#app");
})();
