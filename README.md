# ZYFW 大学生活工具集

ZYFW 是一款面向北师大校园学习场景的 Windows 桌面工具箱。应用采用本地服务与 Edge 浏览器窗口运行，提供教务抢课、SPOC 学习和师道云学习辅助插件。

所有账号配置、日志和插件缓存均保存在本机 `data/` 目录，不会随项目上传。

## 主要功能

| 插件 | 功能 |
|---|---|
| 教务抢课 | 按专业获取课程列表，轮询课程名额并提交选课；支持多课程与候选班级。 |
| SPOC 刷课 | 自动播放 SPOC 课程视频，支持多线程、倍速和 Cookie 登录。 |
| 师道云刷课 | 自动遍历师道云课程视频，根据服务器记录时长判断完成状态并继续学习。 |

## 系统要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- Microsoft Edge
- 与 Edge 版本匹配的 Microsoft Edge WebDriver

## 快速开始

### 1. 获取项目

```powershell
git clone https://github.com/JingHT-BNU/ZYFW.git
cd ZYFW
```

### 2. 创建环境并安装依赖

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. 安装 Microsoft Edge WebDriver

1. 在 Edge 地址栏打开 `edge://settings/help`，记录浏览器版本号。
2. 访问 [Microsoft Edge WebDriver 官方下载页](https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/)。
3. 下载与 Edge 版本、系统架构（通常为 x64）一致的 WebDriver。如果对应版本没找到，可直接更新edge到最新版本然后下载
4. 解压压缩包，将 `msedgedriver.exe` 放到固定目录，例如：

   ```text
   C:\Tools\EdgeDriver\msedgedriver.exe
   ```

5. 启动 ZYFW 后，在“全局设置 → EdgeDriver 路径”中填写该文件的完整路径。

Edge 自动更新后，应重新下载对应版本的 WebDriver，避免出现浏览器启动失败。

### 4. 启动工具箱

双击项目根目录中的 `start.bat`。程序会自动选择可用端口并打开独立的 Edge 应用窗口。

也可以使用命令行启动：

```powershell
.venv\Scripts\python.exe app\main.py --port 8765
```

仅启动服务、不自动打开窗口：

```powershell
.venv\Scripts\python.exe app\main.py --no-window
```

服务实际地址写入 `data/zyfw-gui.url`，运行日志写入 `data/logs/zyfw-gui.log`。

### 5. 完成初始配置

1. 打开“全局设置”，填写北师大统一身份认证账号和密码。
2. 打开目标插件的“配置 / 日志”页面，填写课程链接、运行参数和插件专用账号。
3. 保存配置，返回仪表盘并点击“启动”。
4. 在插件详情页查看运行状态、实时日志、进度条和完成结果。

密码使用 Windows DPAPI 加密存储，界面和 API 中只显示 `******`。插件账号留空时自动使用全局账号。

## 插件配置说明

### 教务抢课

- 首先抓取专业目录，再选择培养层次、年级、学院和专业。
- 点击“开始专业课程抓取”获取课程列表。
- 勾选目标课程，并可填写候选班级；候选班级留空表示不限班级。
- 支持轮询选课和单次选课两种模式。

### SPOC 刷课

- 可粘贴一个或多个课程学习内容页链接。
- 链接留空时，插件将遍历账号下的已选课程。
- 支持未完成筛选、并发浏览器数、播放倍速和无头模式。

### 师道云刷课

- 在插件配置中每行填写一个 `https://bdata.bnu.edu.cn/course?cid=...` 课程链接。
- 插件优先使用页面显示的课程时长要求，并核对服务器记录的观看进度。
- 支持未完成筛选、播放倍速、完成容差和无头模式。

## 数据与安全

- `data/` 保存账号配置、Cookie、课程缓存和日志，已被项目排除。
- 请勿将 `data/`、浏览器配置目录或导出的 Cookie 发送给他人。
- 插件在本机以当前用户权限运行，安装第三方插件前应审查其代码。
- 关闭 GUI 窗口后，本地服务和运行中的插件进程会自动停止。

## 常见问题

**浏览器无法启动**

确认 `msedgedriver.exe` 与当前 Edge 版本一致，并检查全局设置中的驱动路径。

**页面内容没有更新**

完全退出 ZYFW 后重新运行 `start.bat`。主程序修改需要重启服务才能生效。

**端口被占用**

程序会自动选择其他端口；实际地址以 `data/zyfw-gui.url` 或启动日志为准。

**登录失败**

检查统一身份认证账号，并确认对应校园系统当前可以正常访问。

## 插件开发

插件清单、运行时协议、配置表单、自定义 UI、事件上报和安装规范参见 [插件开发指南](PLUGIN_DEVELOPMENT.md)。
