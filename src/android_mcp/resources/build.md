# 构建规则

使用项目自己的 `gradlew.bat`/`gradlew`。任务名必须来自受控模板或项目发现结果，不能传任意 shell 命令。
优先 Debug 变体；Release 任务必须设置 `confirm_release=true`。
