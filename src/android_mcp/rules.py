"""Sectioned Android/Kotlin coding rules."""

from __future__ import annotations

from importlib import resources

from .models import AndroidMcpError, ok


DEFAULT_RULES = {
    "workflow": """# 工作流\n\n构建、写入或审核前先读取对应规则章节；修改 Kotlin、Gradle 或 XML 必须使用 android_file。\n""",
    "env": """# 环境检查\n\n构建前调用 android_environment(action=\"detect\")。优先使用项目 Gradle Wrapper，并注入检测到的 JDK 与 SDK。\n""",
    "writing": """# Kotlin 写入\n\n修改现有文件时每个 edit 都提供 old_content；先 dry_run 预览，再落盘。保持现有命名空间、注释和项目风格。\n""",
    "xml": """# XML 与 Manifest\n\n优先使用结构化动作；避免覆盖整个 Manifest。修改前确认权限、Activity 和 application 属性的现有值。\n""",
    "gradle": """# Gradle\n\n只执行项目发现结果或受控任务模板。Release、签名和发布任务需要显式确认。\n""",
    "build": """# 构建\n\n使用 android_build，不传任意 shell 命令。失败时先读取 diagnostics 与日志尾部，再修改代码并重试。\n""",
    "review": """# 审核\n\n检查路径越权、敏感文件、旧内容校验、依赖版本一致性、构建产物和日志脱敏。\n""",
    "safety": """# 安全\n\n禁止修改 local.properties、密钥库、密码配置、.gradle、build、.idea 与 .git。不要回显密码、Token 或 API Key。\n""",
    "debugging": """# 诊断\n\n按 Kotlin 编译、Gradle 配置、AAPT2、Manifest merger、D8/R8、依赖和环境缺失分类定位。\n""",
    "agent_rules": """# Agent 硬规则\n\nKotlin/Gradle/XML 文件必须用 android_file；复杂 action 先 tool_help；构建前先环境检测；写入后重新 read。\n""",
}


def get_rules(section: str | None = None, language: str = "kotlin") -> dict:
    if language.lower() not in {"kotlin", "android", "java", "gradle"}:
        raise AndroidMcpError(f"不支持的规则语言：{language}", code="unsupported_language")
    if section:
        content = _load_section(section)
        return ok({"language": language, "section": section, "content": content})
    sections = {name: _load_section(name) for name in sorted(DEFAULT_RULES)}
    return ok({"language": language, "sections": sections})


def _load_section(section: str) -> str:
    if section not in DEFAULT_RULES:
        raise AndroidMcpError(
            f"未知编码规则章节：{section}",
            code="unknown_rules_section",
            hint=f"可用章节：{', '.join(sorted(DEFAULT_RULES))}",
        )
    try:
        resource = resources.files("android_mcp").joinpath("resources", f"{section}.md")
        if resource.is_file():
            return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        pass
    return DEFAULT_RULES[section]
