# android-mcp

这是一个运行在 Windows 本地的 MCP 服务，用于安全地检查、编辑、构建和验证 Kotlin/Android 工程，也可以通过 ADB 对已连接的 Android 手机或模拟器执行交互式 UI 自动化。

## 本地运行

~~~powershell
.\scripts\install.ps1
# 如 PowerShell 执行策略禁止脚本：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
~~~

MCP 客户端使用 `.venv\\Scripts\\python.exe -m android_mcp` 作为 stdio 服务。服务更新后需要重启 MCP 连接，避免宿主继续使用旧进程。

首次同步官方 Android 知识库：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync-knowledge.ps1
# 只同步指定来源
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync-knowledge.ps1 -SourceId google.android.api,xiaomi.hyperos.android15
~~~

服务使用 stdio 传输。工具结果统一返回 success/data/error/meta；源码文件修改默认要求 old_content，并支持 dry_run 与历史备份。

## 设备自动化

android_device 保留原有的设备列表、安装、启动、停止、卸载、logcat 和截图能力，并增加：

- screen_size：读取屏幕宽高和旋转方向。
- ui_dump：读取 UIAutomator 层级，返回可用于定位的文本、content-desc、resource-id、class 和 bounds。
- tap / long_press / swipe：按坐标操作，也可以用 selector 按文本或资源 ID 定位节点。
- input_text / press / wait：输入文本、发送常用按键、等待界面稳定。
- wait_for / assert_text：等待或断言 UI 节点。
- run_sequence：一次提交多步交互流程，可在每一步后自动截图。

示例：

~~~json
{
  "action": "run_sequence",
  "project_root": "D:/Android/example",
  "serial": "emulator-5554",
  "steps": [
    {"action": "tap", "selector": "登录", "selector_type": "text"},
    {"action": "input_text", "text": "demo@example.com"},
    {"action": "press", "key": "ENTER"},
    {"action": "wait_for", "text": "首页", "timeout_ms": 5000},
    {"action": "screenshot", "name": "home"}
  ],
  "screenshot_each_step": true
}
~~~

交互动作使用固定的 ADB 命令白名单，不提供任意 shell。每个长操作返回 task_id，用 android_task(action="result") 获取完整结果。多设备连接时必须显式传 serial。

当前实现是“可控输入 + UI 层级感知 + 截图证据”，不是持续的视频流或远程桌面；如果需要实时画面，可以按步骤截图，或后续增加专门的屏幕流传输层。

## 其他能力

- android_environment：发现 JDK、SDK、ADB、Android Studio 和 Gradle Wrapper。
- android_project：发现模块、变体、受控任务、依赖和诊断。
- android_file：安全读写、搜索、替换、格式化和 Kotlin import 管理。
- android_build：通过项目 Gradle Wrapper 异步执行受控构建。
- android_task：查询、长轮询、获取结果和取消异步任务。
- android_kb、get_coding_rules、tool_help、experience：知识检索、规则和辅助服务。

## 知识库与证据规则

`android_kb` 同时检索项目源码、Google/AOSP 与 Xiaomi HyperOS 官方资料；涉及 API、权限、Manifest、依赖、后台或设备兼容性时，先用 `require_citation=true` 获取 `evidence_id`，再把它传给 `android_file`。完整的来源白名单、同步、审计和开发流程见 [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md)。

推荐编码顺序：`android_environment` → `android_project` → `android_kb(scope="project")` → `android_kb(scope="official", require_citation=true)` → `android_file(dry_run=true)` → `android_file(dry_run=false)` → `android_file(read)` → `android_build`。

涉及 API、兼容性、依赖、Manifest、权限、后台或设备行为的写入必须携带 `evidence_ids`；纯格式化只能使用 `android_file(action="format")` 豁免证据。规则章节可通过 `get_coding_rules` 获取。
