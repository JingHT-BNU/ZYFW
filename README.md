# ZYFW 大学生活工具集

带现代 GUI 的校园自动化工具箱。内置插件:

> 面向 AI Agent / 新维护者的交接文档见 [HANDOVER.md](HANDOVER.md)(架构、开发历程、坑位与测试体系)。

| 插件 | 功能 | 改造自 |
|---|---|---|
| `course-grab` 教务抢课 | 轮询 zyfw.bnu.edu.cn 名额并自动提交,支持多课程/多班级候选 | `legacy/dun.py` + `legacy/main.py` |
| `spoc-autoplay` SPOC 刷课 | 多线程播放 spoc.bnu.edu.cn 视频,倍速 + Cookie 免登录 | `legacy/spoc.py` |

## 启动

双击 `start.bat`(首次使用先完成下方安装)。

默认自动选择空闲端口(8765 起)并弹出独立 Edge 窗口;也可命令行指定:

```
python app\main.py --port 8765
python app\main.py --no-window   # 只起服务,浏览器访问打印的地址
```

关闭窗口后,服务在 8 秒宽限期(防页面刷新误判)内无任何客户端重连即自动退出,并停止所有运行中的插件子进程;`--no-window` 方式启动的服务不会自动退出。GUI 运行日志见 `data/logs/zyfw-gui.log`。

## 首次安装(已装过可跳过)

```
pip install -r requirements.txt
```

## 使用流程

1. **全局设置**:填入北师大统一身份认证账号(教务系统 / SPOC 通用,密码 DPAPI 加密存于 `data/global.json`;旧版双账号配置自动迁移)
2. **插件详情 → 插件配置**(教务抢课为自定义界面,与教务系统一致):
   - 选择 培养层次/年级/院(系)/部 → 自动显示已缓存的课程列表 → 勾选要抢的课程(候选班级留空=不限班级);点「开始专业课程抓取」可抓取/更新当前专业的课程缓存,完成后自动列出(无需启动插件)
   - 配置允许为空保存(先设置无头模式等基础选项,课程以后再选);运行模式只有 轮询选课/单次选课 两种
   - 首次打开会提示抓取专业目录(全局共用一份,点击「抓取专业目录」开始;抓取失败会在页面提示并可重试,重试会自动跳过已抓取的组合续抓;瞬时网络中断会自动重启续抓,无需人工干预;抓取中每 50 个组合自动保存进度,配置页显示进度条与预计剩余时间)
   - 更新课程缓存:选好专业后点「开始专业课程抓取」,完成后课程列表自动列出
   - SPOC 刷课 (2.1):粘贴课程**学习内容页**链接(studycontent 页 URL,一行一个);留空则自动遍历全部已选课程
     - 默认只刷未完成视频,已完成的自动跳过
     - 完成判定以播放页"完成度"元素为准(显示"已完成"才计数,服务器没记录不算完成)
     - 运行日志下方按线程分块展示:所刷视频标题、服务器进度条、剩余时长、预计完成时间
     - 某平台账号密码与统一认证不同时,在插件配置中单独填写即可覆盖
3. **仪表盘 → 启动**:实时日志与进度条在插件详情页查看

## 插件开发

任何 selenium 自动化脚本都可打包为插件。结构:

```
my-plugin/
├── plugin.json     # 清单:id/name/version/entry/config_schema
└── plugin.py       # 入口,读 --config <json> --datadir <dir>
```

`config_schema` 声明表单字段,前端自动渲染。支持类型:
`string / password / number / bool / choice / string-list / course-table / specialty`

**自定义配置界面**(可选):在 plugin.json 中声明 `"config_ui": "ui.html"`,提供插件目录内的单个 HTML 文件(不含 `<style>` 与内联样式,风格由宿主 `zy-*` 类统一保证,后端下发前也会强制剥离)。界面脚本通过 `window.ZyfwConfigUI` 与宿主交互:

```js
await ZyfwConfigUI.getConfig()          // 读取配置(密码为 ****** 打码)
await ZyfwConfigUI.saveConfig(obj)      // 保存配置(****** 字段后端保持原值)
await ZyfwConfigUI.api(path, options)   // fetch("/api/"+path), 按 content-type 解析
await ZyfwConfigUI.post(path, body)     // 便捷 POST JSON
ZyfwConfigUI.message(type, text)        // success/warning/error/info 顶部消息
```

未声明 `config_ui` 的插件继续使用 `config_schema` 简易表单,两种方式完全兼容。

插件里 `print()` 即日志;进度用:

```python
from _sdk.sdk import PluginContext
ctx = PluginContext()
ctx.progress(done=3, total=100)      # 前端渲染进度条
ctx.status("已提交选课 GEN06122")     # 前端高亮状态
```

在"插件管理"页上传 zip 即完成安装。

## 目录说明

```
app/          GUI 服务(FastAPI + Vue3 前端)
plugins/      内置插件与 _sdk 公共库;installed/ 存放用户安装的插件
data/         配置(DPAPI 加密)、日志、cookie 缓存
legacy/       旧脚本归档(含明文密码,建议改密后删除)
.venv/        Python 虚拟环境
```

## 测试

```
python app\selftest.py         # DPAPI + 配置存储 + 插件发现
python app\selftest_runner.py  # 子进程启停与事件采集
python app\selftest_api.py     # REST/WebSocket 集成
python app\selftest_catalog.py # 专业目录/课程列表端点(缓存读取与任务守卫)
python app\selftest_catalog_ckpt.py # 目录抓取检查点/断点续抓/自动重启逻辑(离线桩)
node app\selftest_ui_grab_progress.js # 配置界面抓取进度条行为(离线 DOM 桩, 需 Node)
python app\selftest_ui.py      # 真实浏览器渲染冒烟(需先启动服务)
python app\selftest_ui20.py    # SPOC 2.0 配置表单走查(需先启动服务)
python app\selftest_ui21.py    # SPOC 2.1 线程面板走查(需先启动服务,会临时改插件配置)
```
