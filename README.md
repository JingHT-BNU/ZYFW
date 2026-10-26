# ZYFW 大学生活工具集

ZYFW 是一款面向北师大校园学习场景的本地 Web 工具箱。应用在本机启动服务，并通过浏览器界面提供教务抢课和 BNU刷课插件。

所有账号配置、日志和插件缓存均保存在本机 `data/` 目录，不会随项目上传。

## 主要功能

| 插件 | 功能 |
|---|---|
| 教务抢课 | 按专业获取课程列表，轮询课程名额并提交选课；支持多课程与候选班级。 |
| BNU刷课 | 使用同一北师大统一认证账号管理师道云与 SPOC 课程，支持章节筛选、混合并发刷课、师道云自动答题和评论回复。 |

## 环境要求

- Python 3.10 或更高版本
- Microsoft Edge、Mozilla Firefox、Google Chrome 或 Chromium 等现代浏览器中的至少一种
- 使用浏览器自动化插件时，需要与所选插件浏览器版本匹配的 WebDriver
- 推荐使用 Edge，但 Edge 不是运行主窗口的必要条件

ZYFW 的界面不绑定特定浏览器，支持在 Windows、macOS 和常见 Linux 桌面环境中运行。不同平台需要安装对应的 Python、浏览器和 WebDriver。

## 快速开始

### 1. 获取项目

```console
git clone https://github.com/JingHT-BNU/ZYFW.git
cd ZYFW
```

### 2. 创建环境并安装依赖

```console
# Windows
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt

# macOS / Linux
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Ubuntu 如果缺少 `venv` 模块，可先安装 `python3-venv`。

### 3. 安装 WebDriver

插件运行浏览器可在全局设置中切换，与应用主窗口使用的浏览器相互独立。主窗口优先使用 Microsoft Edge 应用模式；未安装 Edge 时，自动使用系统默认浏览器。

选择一种浏览器并安装对应驱动：

#### Microsoft Edge

1. 在 Edge 地址栏打开 `edge://settings/help`，记录浏览器版本号。
2. 访问 [Microsoft Edge WebDriver 官方下载页](https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/)。
3. 下载与 Edge 版本、操作系统和系统架构一致的 WebDriver。如果对应版本未提供，可先更新 Edge，再重新下载。
4. 将驱动文件放到项目根目录或任意固定目录；Windows 下的文件名通常为 `msedgedriver.exe`。

#### Mozilla Firefox

1. 访问 [GeckoDriver 官方发布页](https://github.com/mozilla/geckodriver/releases)。
2. 下载适用于当前操作系统和系统架构的 GeckoDriver。
3. 解压后将驱动文件放到项目根目录或任意固定目录；Windows 下的文件名通常为 `geckodriver.exe`。

#### Chromium / Chrome for Testing

1. 访问 [Chrome for Testing 官方下载页](https://googlechromelabs.github.io/chrome-for-testing/)。
2. 选择与所需版本、操作系统和系统架构一致的构建。
3. 下载 Chromium 与 ChromeDriver 对应平台的压缩包。
4. 将 Chromium 解压到项目目录或任意固定目录。
5. 将 ChromeDriver 解压到项目目录或任意固定目录。

项目根目录中的浏览器驱动、Chromium 程序和浏览器配置目录均不会进入版本控制。

启动 ZYFW 后，在“全局设置 → 插件运行浏览器”中选择 Edge、Firefox 或 Chromium。驱动和浏览器位于当前平台可自动识别的位置时可留空路径；使用其他位置时，填写对应的完整路径。

浏览器自动更新后，应同步更新对应 WebDriver。

### 4. 启动工具箱

Windows 可双击项目根目录中的 `start.bat`。程序会自动选择可用端口，优先打开独立的 Edge 应用窗口；未安装 Edge 时，回退到系统默认浏览器。

macOS 或 Linux 使用 `start.sh`：

```console
chmod +x start.sh
./start.sh
```

也可以使用命令行启动：

Windows 命令行：

```console
.venv\Scripts\python.exe app\main.py --port 8765
```

macOS / Linux 命令行：

```console
.venv/bin/python app/main.py --port 8765
```

仅启动服务、不自动打开窗口时，在命令末尾增加 `--no-window`。

服务实际地址写入 `data/zyfw-gui.url`，运行日志写入 `data/logs/zyfw-gui.log`。

### 5. 完成初始配置

1. 打开“全局设置”，填写北师大统一身份认证账号和密码。
2. 在“插件运行浏览器”中选择 Edge、Firefox 或 Chromium，并确认对应驱动路径。
3. 打开目标插件的“配置 / 日志”页面，填写运行参数；BNU刷课只使用全局统一认证账号。
4. 保存配置，返回仪表盘并点击“启动”。
5. 在插件详情页查看运行状态、实时日志、进度条和完成结果。

所有平台统一使用 Fernet 加密凭据。密钥保存在 `data/.credentials.key`，并设置为仅当前用户可读写。界面和 API 中只显示 `******`。`data/` 和凭据密钥均不得分享或提交。

从旧版加密配置升级时，无法转换的密码字段会清空，需要在全局设置中重新输入一次。

## 插件配置说明

### 教务抢课

- 首先抓取专业目录，再选择培养层次、年级、学院和专业。
- 点击“开始专业课程抓取”获取课程列表。
- 勾选目标课程，并可填写候选班级；候选班级留空表示不限班级。
- 支持轮询选课和单次选课两种模式。

### BNU刷课

- 点击“获取课程详情”后，插件使用全局北师大统一认证账号分别登录师道云和 SPOC，并自动爬取账号下的全部课程。
- 课程缓存按统一认证账号隔离。切换账号后再次获取，会新增或更新该账号自己的缓存；旧账号缓存不会被新账号覆盖。
- 师道云课程可选择“刷指定任务”“刷全部任务”“全部任务 + 任务外视频”或“刷指定章节”。
- SPOC 课程可选择“刷全部课程”或“刷指定章节”；无学习内容的课程只保存在缓存中，不显示在课程选择列表。
- 师道云缓存包含任务、章节、视频、测试、评论回复要求、任务外视频和完成情况；SPOC 缓存包含章节、全部视频、作业/测试及完成情况。
- 同一任务队列可混合执行 SPOC 与师道云视频，每个浏览器进程可处理任一平台的视频。
- 师道云可使用 1–3 倍稳定播放、完成容差和未完成筛选；SPOC 同时复用并发浏览、倍速和未完成筛选。
- 完成状态只使用播放页服务器时长文字与视频总时长比较。
- 本地视频播放结束但服务器时长未满时，会自动从头重播；进程面板会显示重播状态和次数。
- 插件详情页以每行两个卡片显示各进程的平台、课程、视频、服务器进度和预计剩余时间。

师道云的“自动答题”和“评论回复”可分别启用；启用任一项后，需要填写兼容 OpenAI Chat Completions 的 API 地址、API Key 和模型 ID。视频刷课与答题互动会并发执行。

- 自动处理尚未提交的测验，包括单选、多选、简答和量表题。
- 量表题不需要逐题确认，也不会出现在答案核验列表；插件会直接按策略填写。可选择两种策略：“固定正负策略”可分别设置正向题和负向题的固定选项，默认分别为“同意”和“不同意”；“正态分布策略”以“一般”为均值，约 2 倍标准差映射为“同意/不同意”，约 3 倍标准差映射为“非常同意/非常不同意”。
- 从课程评论中随机取样，生成一条评论和一条回复；模型输出去除 Markdown 和常见 AI 身份化开场白。
- 默认情况下，模型内容先显示在插件配置页中，允许修改；只有点击“确认并提交”后，插件才会填写并提交网页内容。
- 启用“全自动模式”后会跳过人工核验并直接提交。启用开关时和每次启动任务前都会弹出危险警告并要求确认。
- 提交测验后会检查“提交问卷”颜色是否变为绿色，并再次核对任务状态。

## 数据与安全

- `data/` 保存账号配置、Cookie、课程缓存和日志，已被项目排除。
- 统一认证密码和模型 API Key 均使用 Fernet 加密；界面与 API 只显示 `******`。
- 请勿将 `data/`、浏览器配置目录或导出的 Cookie 发送给他人。
- 插件在本机以当前用户权限运行，安装第三方插件前应审查其代码。
- 关闭 GUI 窗口后，本地服务和运行中的插件进程会自动停止。

## 常见问题

**浏览器无法启动**

确认全局设置中选择的浏览器与 WebDriver 版本一致，并检查对应驱动和 Chromium 可执行文件路径。

**页面内容没有更新**

完全退出 ZYFW 后重新启动服务。Windows 使用 `start.bat`，macOS/Linux 使用 `start.sh`；主程序修改需要重启服务才能生效。

**端口被占用**

程序会自动选择其他端口；实际地址以 `data/zyfw-gui.url` 或启动日志为准。

**登录失败**

检查统一身份认证账号，并确认对应校园系统当前可以正常访问。

## 插件开发

插件清单、运行时协议、配置表单、自定义 UI、事件上报和安装规范参见 [插件开发指南](PLUGIN_DEVELOPMENT.md)。

## 开源协议

本项目原创源代码采用 WTFPL Version 2 协议，完整条款参见 [LICENSE](LICENSE)。第三方组件继续遵循其各自的许可证。
