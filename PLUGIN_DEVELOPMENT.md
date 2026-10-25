# ZYFW 插件开发指南

本文档面向需要扩展 ZYFW 功能的开发者，说明插件目录、清单协议、运行时通信、配置持久化、浏览器自动化、自定义配置界面和打包安装方式。

## 1. 总体架构

ZYFW 宿主由 FastAPI 本地服务和静态 Vue 前端组成。插件不是宿主进程中的 Python 模块，而是由宿主按需启动的独立子进程。

```text
浏览器界面
    │ REST / WebSocket
    ▼
FastAPI 宿主
    │ 启动子进程并读取 stdout
    ▼
plugins/<plugin-id>/plugin.py
```

这种设计具有以下约束：

- 同一 `plugin_id` 同时只能存在一个运行实例。
- 插件通过标准输出发送日志、状态和进度。
- 插件进程退出后，宿主停止推送进度并广播终态。
- 插件停止时，宿主先尝试温和终止；超时后结束其进程树。
- 插件文件在每次启动时从磁盘加载，修改插件代码后重新运行即可生效。

修改 `app/` 下的宿主代码后，需要完全退出并重新启动 ZYFW。

## 2. 插件目录与清单

内置插件位于 `plugins/<plugin-id>/`。建议目录结构如下：

```text
plugins/
└── example-plugin/
    ├── plugin.json
    ├── plugin.py
    └── ui.html
```

`ui.html` 为可选文件。入口文件应使用 Python 编写，并自行管理所需的浏览器或外部命令。

### plugin.json

清单必须包含以下字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 唯一插件标识，最长 64 字符，仅允许字母、数字、`-` 和 `_`。 |
| `name` | string | 界面显示名称。 |
| `version` | string | 插件版本，例如 `1.0.0`。 |
| `entry` | string | 插件目录内的单个 Python 文件。 |
| `description` | string | 插件功能摘要。 |
| `config_schema` | array | 配置字段定义；无字段时使用空数组。 |
| `config_ui` | string | 可选的单文件 HTML 自定义配置界面。 |

示例：

```json
{
  "id": "example-plugin",
  "name": "示例插件",
  "version": "1.0.0",
  "description": "演示标准插件结构。",
  "entry": "plugin.py",
  "config_schema": [
    {
      "key": "target_url",
      "label": "目标地址",
      "type": "string",
      "default": ""
    },
    {
      "key": "headless",
      "label": "无头模式",
      "type": "bool",
      "default": true
    }
  ]
}
```

插件 ID 会用于配置目录、数据目录和进程槽位，因此发布后不宜随意修改。内置插件 ID 受保护，用户插件不能覆盖。

## 3. 运行时协议

宿主使用以下形式启动插件：

```text
python plugin.py --config <runtime-config.json> --datadir <plugin-data-dir>
```

`--config` 是本次运行的有效配置，`--datadir` 是插件专属数据目录。插件公共 SDK 会解析这两个参数：

```python
from _sdk.sdk import PluginContext


def main() -> None:
    ctx = PluginContext()
    ctx.log(f"开始运行，数据目录为 {ctx.datadir}")


if __name__ == "__main__":
    main()
```

`PluginContext` 提供：

| 成员 | 用途 |
|---|---|
| `ctx.config` | 本次运行的完整配置字典。 |
| `ctx.cfg(key, default)` | 读取单个配置值。 |
| `ctx.username` / `ctx.password` | 读取统一身份认证凭据。 |
| `ctx.datadir` | 插件数据目录。 |
| `ctx.log(message)` | 输出带时间前缀的普通日志。 |
| `ctx.progress(**fields)` | 输出结构化进度事件。 |
| `ctx.status(text, **fields)` | 输出状态事件；`done=True` 表示任务完成。 |
| `ctx.emit(event)` | 输出自定义结构化事件。 |

插件配置为空字符串的账号或密码会回退到全局统一身份认证账号。插件配置中的非空账号优先。

## 4. 配置字段与表单

当插件不提供 `config_ui` 时，宿主根据 `config_schema` 自动生成配置表单。

支持的字段类型如下：

| 类型 | 用途 |
|---|---|
| `string` | 普通文本。 |
| `password` | 密码文本；读取时打码为 `******`。 |
| `number` | 数字，可设置 `min`、`max` 和 `step`。 |
| `bool` | 布尔开关。 |
| `choice` | 单选；使用 `choices`，可通过 `choice_labels` 提供显示文本。 |
| `string-list` | 字符串列表，界面以多行文本编辑。 |
| `course-table` | 表格型课程配置。 |
| `specialty` | 专业级联配置；通常配合自定义界面使用。 |

字段示例：

```json
{
  "key": "speedup",
  "label": "播放倍速",
  "type": "number",
  "default": 2,
  "min": 1,
  "max": 8,
  "step": 0.5
}
```

建议为所有字段提供合理的默认值，并确保插件在配置缺失或类型异常时仍能给出明确日志。

## 5. 日志、状态与进度

插件标准输出按行读取。非结构化文本会显示为普通日志；单行 JSON 且包含 `__event__` 时，会转换为结构化事件。

### 普通日志

```python
ctx.log("正在打开课程页面")
```

### 状态事件

```python
ctx.status("已登录", stage="login")
ctx.status("任务完成", done=True)
```

### 进度事件

```python
ctx.progress(done=3, total=10, title="当前任务")
```

推荐使用以下字段：

| 字段 | 说明 |
|---|---|
| `done` | 已完成数量。 |
| `total` | 总数量。 |
| `title` | 当前处理对象。 |
| `percent` | 百分比进度。 |
| `eta_seconds` | 预计剩余秒数。 |

每个事件必须单独占一行，并立即刷新输出。SDK 的 `ctx.log`、`ctx.status` 和 `ctx.progress` 已处理刷新，插件应优先使用这些方法。

最近一次进度会通过插件列表 API 暴露给前端；进程结束后自动清除。日志中不得输出密码、Cookie、令牌或其他敏感值。

## 6. Selenium 与浏览器

公共 SDK 提供统一的浏览器初始化工具：

```python
from _sdk.sdk import make_driver


driver = make_driver(ctx, headless=True)
try:
    driver.get("https://example.com")
finally:
    driver.quit()
```

`make_driver` 的浏览器行为：

- 根据全局设置 `general.browser` 选择 `edge`、`firefox` 或 `chromium`。
- 开启自动播放许可，并减少后台标签页节流。
- 有界面模式下最大化浏览器窗口；无头模式使用 1920×1080 视口。
- Edge 使用 `general.driver_path`；Firefox 使用 `general.geckodriver_path`；Chromium 使用 `general.chromedriver_path`。
- Chromium 浏览器可执行文件通过 `general.chromium_path` 指定。
- 路径留空时依次检测项目根目录的本地文件和 Selenium Manager。
- Chromium 模式要求存在有效的 Chromium 可执行文件。

驱动安装方式和默认目录参见项目 README。插件代码不需要按浏览器分支，统一调用 `make_driver` 即可。

### 元素等待

SDK 提供两类查找方式：

```python
from _sdk.sdk import find, find_strict

element = find(driver, '//*[@id="optional"]', timeout=5)
required = find_strict(driver, '//*[@id="required"]', timeout=10)
```

`find` 超时后返回 `None`，`find_strict` 超时后抛出异常。插件应根据元素是否必需选择合适方法。

自动化流程应尽量等待页面状态、URL、文本或元素出现，而不是依赖固定长时间休眠。页面会异步重渲染时，应增加有限次数的重试，并在失败日志中说明定位条件。

浏览器必须在 `finally` 中关闭。长时间任务应检查进程终止、页面跳转和登录失效，并避免无上限重试。

## 7. 自定义配置界面

复杂插件可提供单文件 `ui.html`，并在清单中声明：

```json
{
  "config_ui": "ui.html"
}
```

自定义界面的约束：

- 只能引用插件目录内的单个 HTML 文件。
- 不允许自带 `<style>` 或内联样式。
- 配色、间距、按钮和表格应使用宿主提供的 `zy-*` 类。
- 后端下发界面前会移除插件样式。
- 界面脚本会重新创建执行，因此应处理初始化失败和重复挂载。

宿主注入 `window.ZyfwConfigUI` 桥接对象：

```javascript
const config = await ZyfwConfigUI.getConfig();
await ZyfwConfigUI.saveConfig(config);

const plugins = await ZyfwConfigUI.api("plugins");
await ZyfwConfigUI.post("plugins/example-plugin/run", { value: 1 });
ZyfwConfigUI.message("success", "操作完成");
```

常用桥接方法：

| 方法 | 说明 |
|---|---|
| `getConfig()` | 获取插件配置，密码已打码。 |
| `saveConfig(config)` | 保存完整配置，`******` 会保持原密码。 |
| `api(path, options)` | 请求 `/api/<path>`，按响应类型解析。 |
| `post(path, body)` | 发送 JSON POST 请求。 |
| `message(type, text)` | 显示成功、警告、错误或信息消息。 |

自定义界面脚本应避免依赖同级兄弟节点的固定顺序，并对可能延迟出现的元素做空值判断。

## 8. 插件数据目录

宿主为每个插件创建独立数据目录：

```text
data/plugins/<plugin-id>/
```

该目录适合保存 Cookie、缓存、检查点或插件运行状态。数据处理约定如下：

- 使用原子写入，避免进程终止后留下半个 JSON 文件。
- 写入临时文件时使用唯一名称，并在替换后清理临时文件。
- 不保存与当前任务无关的个人数据。
- 不将数据目录内容复制到插件安装包。
- 数据目录不属于源码，也不应提交到版本控制。

宿主配置中的密码、密钥和令牌由 Fernet 统一加密，加密密钥保存在 `data/.credentials.key`。插件不得读取、复制或记录该密钥。

运行时配置由宿主临时生成并在插件结束后清理。插件不应将其作为长期配置文件使用。

## 9. 打包与安装

插件通过“插件管理”页面上传 ZIP 包。ZIP 可采用以下两种布局：

```text
example-plugin.zip
├── plugin.json
└── plugin.py
```

或在根目录包含一个插件文件夹：

```text
example-plugin.zip
└── example-plugin/
    ├── plugin.json
    └── plugin.py
```

安装规则：

- ZIP 中必须恰好存在一个有效 `plugin.json`。
- 不允许绝对路径或 `..` 路径。
- 单个插件包最多 500 个文件，解压后最大 50 MB。
- 插件 ID 不得与内置插件冲突。
- 用户插件安装到 `plugins/installed/<plugin-id>/`。
- 删除用户插件不会自动删除其 `data/plugins/<plugin-id>/` 数据。

安装前应检查清单、入口代码和第三方依赖。插件以当前操作系统用户权限运行，具备读取本地文件和执行命令的能力。

## 10. 生命周期与故障处理

插件运行状态由宿主维护。常见状态包括：

- `running`：进程已启动。
- `stopping`：停止命令已发送。
- `exited`：进程正常退出。
- `crashed`：进程以非零状态退出。
- `stopped`：进程由宿主停止。

开发插件时应重点处理：

1. 页面加载超时和登录失效。
2. 浏览器驱动版本不匹配。
3. 网站结构调整导致的元素查找失败。
4. 任务中途停止后的浏览器与临时文件清理。
5. 网络抖动时的有限重试和可恢复进度。
6. 同一插件被重复启动时的状态冲突。

排查运行问题时，应结合插件详情页日志、宿主日志 `data/logs/zyfw-gui.log` 和浏览器当前页面状态。日志应提供具体步骤、目标元素和失败原因，但不得包含账号密码或完整认证 Cookie。
