# android-mcp

这是一个运行在 Windows 本地的 MCP 服务，用于安全地检查、编辑、构建和验证 Kotlin/Android 工程，也可以通过 ADB 对已连接的 Android 手机或模拟器执行交互式 UI 自动化。

## 本地运行

~~~powershell
.\scripts\install.ps1
# 如 PowerShell 执行策略禁止脚本：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
~~~

MCP 客户端使用 `.venv\\Scripts\\python.exe -m android_mcp` 作为 stdio 服务；该命令加载
虚拟环境中的已安装包，不直接加载本工作区的 `src` 目录。
**推荐用 `python -m android_mcp`，不要用 `android-mcp.exe` 入口**：Windows 下
pip 重装会被正在运行的 `.exe` shim 锁文件而失败，`python -m` 方式不受影响，
是自更新 `upgrade` 能可靠工作的前提。服务更新后需要重启 MCP 连接，避免宿主
继续使用旧进程。

`scripts\install.ps1` 默认从远端 Git 仓库安装（非 editable）：

~~~powershell
.\scripts\install.ps1
# 也可以手动指定仓库分支或提交：
.\scripts\install.ps1 -Repository "https://github.com/Yang-Ya-Chao/android-mcp.git" -Revision main
~~~

本地工作区只用于修复、测试和提交；修复完成后先提交并推送 Git，再运行安装脚本或
`android_mcp_update(action="upgrade")`。不要把工作区做成 editable 安装。可以用下面的
命令核验当前运行包来自哪里：

~~~powershell
& .\.venv\Scripts\python.exe -m pip list --editable
& .\.venv\Scripts\python.exe -c "import android_mcp, importlib.metadata as m; print(android_mcp.__file__); print(m.version('android-mcp')); print(m.distribution('android-mcp').read_text('direct_url.json'))"
~~~

第一条命令应无输出；第二条应显示 `.venv\\Lib\\site-packages` 和 Git 的
`direct_url.json`。若从源码修复但尚未推送，不能作为 MCP 运行版本。

服务本身提供版本检查与自更新：

- `android_mcp_update(action="check")`：读取远端 `main` 分支的 `__version__`，
  与本地比对，返回 `latest_version` 与 `update_available`（升级版本前先改
  `src/android_mcp/__init__.py` 并推送到远端）。
- `android_mcp_update(action="upgrade")`：有更新时自动
  `pip install --upgrade git+...` 到运行中的 venv，成功后服务 `exit(0)` 退出，
  宿主客户端会自动重启以加载新版本。

首次同步官方 Android 知识库：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync-knowledge.ps1
# 只同步指定来源
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync-knowledge.ps1 -SourceId google.android.api,xiaomi.hyperos.android15
~~~

服务使用 stdio 传输。工具结果统一返回 success/data/error/meta；源码文件修改默认要求 old_content，并支持 dry_run 与历史备份。

## 设备自动化

android_device 面向 Windows 主机上的 Android 真机和模拟器，保留设备列表、安装、启动、停止、卸载、logcat 和截图能力，并增加：

- screen_size / get_orientation / set_orientation：读取或设置屏幕尺寸、密度和方向。
- ui_dump / list_elements / snapshot：读取 UIAutomator 层级、结构化交互元素，或同时获取 UI 与截图。
- tap / double_tap / long_press / swipe / drag：按坐标或 selector 操作，支持节点 index 和方向滑动。
- input_text / press / open_url / wait：输入文本、发送按键、打开安全 URL、等待界面稳定；非 ASCII 输入可选用 DeviceKit 兜底。
- wait_for / assert_text：等待或断言 UI 节点。
- list_apps / list_packages / package_intents：读取可启动应用、已安装包和包的非数据 Intent。
- start_screen_recording / stop_screen_recording：保存自动化过程的 MP4 证据。
- run_sequence：一次提交多步交互流程，可在每一步后自动截图。

示例：

~~~json
{
  "action": "run_sequence",
  "project_root": "D:/Android/example",
  "serial": "emulator-5554",
  "steps": [
    {"action": "tap", "selector": "登录", "selector_type": "text", "index": 0},
    {"action": "input_text", "text": "demo@example.com", "submit": true},
    {"action": "press", "key": "ENTER"},
    {"action": "wait_for", "text": "首页", "timeout_ms": 5000},
    {"action": "screenshot", "name": "home"}
  ],
  "screenshot_each_step": true
}
~~~

交互动作使用固定的 ADB 命令白名单，不提供任意 shell。每个长操作返回 task_id，用 android_task(action="result") 获取完整结果。多设备连接时必须显式传 serial。

当前实现是“可控输入 + UI 层级感知 + 截图/录屏证据”，不是持续的视频流或远程桌面；如需实时画面，应使用 snapshot 或按步骤截图。大截图会保存在运行时目录并只返回 path，避免 MCP JSON 过大。

## 其他能力

- android_environment：发现 JDK、SDK、ADB、Android Studio 和 Gradle Wrapper。
- android_project：发现模块、变体、受控任务、依赖和诊断。
- android_file：安全读写、搜索、替换、格式化和 Kotlin import 管理。
- android_build：通过项目 Gradle Wrapper 异步执行受控构建。
- android_task：查询、长轮询、获取结果和取消异步任务。
- android_kb、get_coding_rules、tool_help、experience：知识检索、规则和辅助服务。

## 知识库与证据规则

`android_kb` 同时检索项目源码、Google/AOSP 与 Xiaomi HyperOS 官方资料，也支持只读检索 GitHub 开源实现。涉及 API、权限、Manifest、依赖、后台或设备兼容性时，先用官方来源的 `require_citation=true` 获取 `evidence_id`；算法、数据结构和实现对比可以使用 `scope="github"` 获取非官方证据，再把它传给 `android_file`。完整的来源层级、同步、审计和开发流程见 [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md)。

推荐编码顺序：`android_environment` → `android_project` → `android_kb(scope="project")` → 按变更类型选择 `scope="official"` 或 `scope="github"` → `android_file(dry_run=true)` → `android_file(dry_run=false)` → `android_file(read)` → `android_build`。

涉及 API、兼容性、依赖、Manifest、权限、后台或设备行为的写入必须携带 `evidence_ids`；纯格式化只能使用 `android_file(action="format")` 豁免证据。规则章节可通过 `get_coding_rules` 获取。

GitHub 检索通过固定的 GitHub REST API 读取代码，结果会记录仓库、分支、文件定位、blob SHA、许可证、抓取时间和内容哈希。建议配置 `GITHUB_TOKEN`（或 `GH_TOKEN`）以提高代码搜索稳定性；Token 只从环境变量读取，不会写入索引、证据或日志。GitHub 属于非官方来源，不能单独证明 Android 平台契约、权限、厂商行为或版本兼容性。
