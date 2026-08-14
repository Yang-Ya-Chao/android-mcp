# Android 设备测试

## 基本流程

1. 用 android_device(action="list") 检查设备。连接多台设备时必须传 serial。
2. 构建并安装 Debug APK，再用 android_device(action="launch") 启动目标应用。
3. 用 screen_size 或 ui_dump 读取当前屏幕和可访问节点。
4. 用 tap、long_press、swipe、input_text、press 和 wait 执行交互。
5. 用 wait_for 和 assert_text 做界面断言，用 screenshot 保存证据。
6. 多步流程使用 run_sequence，一次提交并用 android_task 获取结果。

## 定位方式

tap 和 long_press 支持两种方式：

- 传 x、y 坐标。服务会先读取屏幕尺寸，拒绝超出屏幕的坐标。
- 传 selector 和 selector_type。selector_type 支持 text、content_desc、resource_id、class_name、package；match 支持 contains 和 equals。

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
    {"action": "tap", "selector": "设置", "selector_type": "text"},
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
- ui_dump 返回最多 500 个节点，原始 XML 只有显式请求 include_xml=true 时返回。
- 卸载和清理 log 仍需要 confirm=true。
- 当前能力提供输入控制、UI 感知和截图证据，不提供持续视频流或远程桌面。
