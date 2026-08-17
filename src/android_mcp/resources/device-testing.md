# Android 设备测试

## 基本流程

1. 用 android_device(action="list") 检查 Windows 主机上的 Android 真机或模拟器；连接多台设备时必须传 serial，也可以用 connect/disconnect 管理 Wi-Fi ADB。
2. 构建并安装 Debug APK，再用 android_device(action="launch") 启动目标应用。
3. 用 screen_size、get_orientation、ui_dump 或 list_elements 读取当前屏幕和可访问节点；需要同时获取 UI 与截图时使用 snapshot。
4. 用 tap、double_tap、long_press、swipe、drag、input_text、press、open_url 和 wait 执行交互。
5. 用 wait_for 和 assert_text 做界面断言，用 screenshot 或 include_image=true 保存/返回截图证据。
6. 多步流程使用 run_sequence，一次提交并用 android_task 获取结果；需要视频证据时使用 start_screen_recording/stop_screen_recording。

## 定位方式

tap、double_tap 和 long_press 支持两种方式：

- 传 x、y 坐标。服务会先读取屏幕尺寸，拒绝超出屏幕的坐标。
- 传 selector 和 selector_type。selector_type 支持 text、content_desc、resource_id、class_name、package；match 支持 contains 和 equals。
- selector 可以配合 index 选择同名节点；resource_id 同时支持完整值（如 `demo:id/login`）和短值（如 `login`）。

swipe 支持坐标模式和方向模式：`direction` 可选 `up`、`down`、`left`、`right`，可以省略起点让服务从屏幕中心滑动，也可以用 `distance` 指定距离。

`list_apps` 返回可启动应用，`list_packages` 返回已安装包，`package_intents` 返回应用的非数据 Intent，`open_url` 只允许 http/https URL。屏幕方向支持 `get_orientation` 和 `set_orientation`。

selector 定位依赖 Android UIAutomator 可访问性树。自绘 Canvas、没有语义节点的图片控件或被遮挡的元素，应改用坐标或在应用中补充可访问语义。

## 自动化流程示例

~~~json
{
  "action": "run_sequence",
  "serial": "emulator-5554",
  "steps": [
    {"action": "tap", "x": 540, "y": 1800},
    {"action": "swipe", "x": 540, "y": 1600, "x2": 540, "y2": 500, "duration_ms": 450},
    {"action": "wait_for", "text": "设置", "timeout_ms": 5000},
    {"action": "tap", "selector": "设置", "selector_type": "text", "index": 0},
    {"action": "double_tap", "x": 540, "y": 900},
    {"action": "swipe", "direction": "up", "distance": 800, "duration_ms": 700},
    {"action": "assert_text", "text": "系统"}
  ],
  "screenshot_each_step": true
}
~~~

## 安全边界

- 不开放任意 adb shell；动作只映射到固定的设备命令。
- run_sequence 默认最多 50 步，单步等待最多 60 秒。
- press 只允许常用导航、输入和音量按键。
- 输入文本限制为 2048 个不含换行和 null 的字符。
- ASCII 输入使用固定的 `adb shell input text`；非 ASCII 输入仅在检测到 Mobile Next DeviceKit 时通过固定广播/粘贴流程处理，否则返回明确错误。
- ui_dump/list_elements 返回最多 500 个节点，节点包含 rect、center、交互属性；原始 XML 只有显式请求 include_xml=true 时返回；截图超过 5 MB 时只返回 path，不把图片塞入 MCP JSON。
- 卸载和清理 log 仍需要 confirm=true。
- 录屏只写入 MCP 运行时 recordings 目录，不接受任意输出路径；当前能力仍不是持续视频流或远程桌面。

## 对比参考

本实现只针对 Windows + Android，参考了以下公开项目的 Android 侧能力设计，不复制其跨平台运行时或任意 shell 能力：

- [CursorTouch/Android-MCP](https://github.com/CursorTouch/Android-MCP)：Accessibility/UIAutomator 状态、元素选择、双击、通知和设备连接体验。
- [mobile-next/mobile-mcp](https://github.com/mobile-next/mobile-mcp)：方向滑动、应用管理、方向切换、截图/录屏和结构化元素输出。
- [minhalvp/android-mcp-server](https://github.com/minhalvp/android-mcp-server)：应用包列表、UI 布局和包 Intent 查询。
