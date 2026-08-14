# Android MCP 知识库与规则引擎规范

本项目的 Android MCP 采用“项目源码 + 受控官方资料 + 可复核证据”的知识库模型。目标是让编码过程先检索依据、再执行写入，并且让每次敏感变更都能追溯到来源、版本和内容哈希。

## 1. 知识来源

官方来源目录位于 `src/android_mcp/resources/official_sources.json`，当前覆盖：

- Google Android 开发文档、API Reference、兼容性、架构建议、Kotlin 风格和 Samples；
- AOSP CDD、AOSP 下载说明、`frameworks/base` 和 AndroidX 源码仓库元数据；
- Xiaomi HyperOS 应用开发、Android 15、通知、后台/多窗口、64 位、大屏和小组件资料。

来源是 HTTPS 白名单，不允许 MCP 调用者把任意 URL 作为抓取代理。AOSP/AndroidX 大型仓库默认保存仓库元数据，不盲目克隆全部源码；需要具体源码时根据目录中的官方仓库链接和版本进行定向获取。

同步后的官方索引默认位于运行时目录的 `knowledge/official-index.json`，项目索引位于项目运行时目录的 `kb-index.json`。运行时目录由 `ConfigManager` 管理，不写入项目源码目录。

首次安装或需要刷新全部官方文档时，可以执行：

```powershell
.\scripts\sync-knowledge.ps1
```

也可以只同步任务需要的来源：

```powershell
.\scripts\sync-knowledge.ps1 -SourceId google.android.api,xiaomi.hyperos.android15
```

脚本只同步白名单中的 HTTPS 来源；AOSP/AndroidX 的 `source_repository` 条目默认只保存仓库元数据。

## 2. MCP 调用流程

```text
android_environment(detect)
        ↓
android_project(discover)
        ↓
android_kb(search, scope=project)
        ↓
android_kb(search, scope=official, require_citation=true)
        ↓
android_file(edit, evidence_ids=[...], dry_run=true)
        ↓
android_file(edit, evidence_ids=[...], dry_run=false)
        ↓
android_file(read) → android_build → android_task(result)
```

官方资料尚未建立索引时，先调用：

```json
{"action":"sync_sources","source_ids":["google-android-develop","xiaomi-app-develop"]}
```

同步是异步任务，使用 `android_task(action="result", task_id="...")` 获取结果。全量同步可能较慢，日常开发优先选择与任务相关的来源。

检索示例：

```json
{
  "action": "search",
  "query": "notification permission Android 13",
  "scope": "official",
  "api_level": 33,
  "require_citation": true,
  "top_k": 8
}
```

小米/HyperOS 任务增加 `vendor="xiaomi"`；需要同时看项目实现和官方契约时使用 `scope="all"`，并在结果中区分 `source=project` 与 `source=official`。

## 3. 证据与规则闸门

`android_kb(search, require_citation=true)` 会生成 `evidence_id`，保存检索结果的来源、URL、定位信息、版本/API level、抓取时间和内容哈希。`android_file` 的代码写入必须传入这些 `evidence_ids`。

规则引擎按变更类型执行以下策略：

| 变更类型 | 证据要求 |
| --- | --- |
| 普通业务代码 | 至少有项目源码/测试证据 |
| API、兼容性、依赖、Manifest、权限、后台、设备行为 | 至少有 Google/AOSP 或 Xiaomi 官方证据 |
| 小米/HyperOS 适配 | 必须包含 Xiaomi 官方证据，并在目标 ROM 验证 |
| 纯格式化/注释 | 可不带证据，但仍受文件安全和旧内容校验约束 |

写入前会检查 evidence 是否存在、引用的记录是否仍在当前索引、内容哈希是否一致。证据失效或不足时拒绝写入，返回重新检索提示；不得用模型记忆或搜索摘要替代证据。

项目审计文件：

- `knowledge/evidence.json`：检索证据快照；
- `knowledge/rule-audit.jsonl`：每次写入规则检查结果。

## 4. 开发规范

1. Android/Kotlin/Gradle/XML 文件统一通过 AndroidMCP 的 `android_file` 操作；复杂 action 先调用 `tool_help`。
2. 修改现有内容必须先 `read`，每个 edit 提供 `old_content`；先 `dry_run=true`，确认 diff 后再落盘。
3. 涉及版本、平台 API、权限、Manifest、后台、通知、设备差异的任务，先检索官方文档和源码，并锁定 `api_level`、`target_sdk`、设备系统版本。
4. 小米资料用于识别 OEM 差异，不能把单一机型结论推广为所有 Android 设备；目标 ROM 上必须验证冷启动、后台恢复、通知、权限拒绝和升级场景。
5. 写入后重新 `android_file(read)`，随后使用 `android_build`；构建失败先读取 diagnostics 和日志尾部，再修改。
6. 发布或安全敏感变更要检查密钥、Token、日志脱敏、导出组件、权限最小化和依赖来源；禁止把凭据放入知识库。

规则章节可通过 `get_coding_rules` 获取：`kb_search`、`evidence`、`architecture`、`xiaomi`、`workflow`、`writing`、`build`、`review` 和 `safety` 等。

## 5. 维护规范

- 每次官方页面结构或版本发生变化后重新同步，保留索引中的 `fetched_at`、`updated_at`、`last_modified` 和 `content_hash`。
- 同步失败保留上一版可用记录，并把错误返回给任务结果；不能把失败页面标记为最新依据。
- 新增官方来源必须先修改白名单、补充权威性和许可证链接，再增加测试；不接受任意用户 URL。
- 代码审查以 evidence_id 为入口复核来源和定位；无法复核的实现应降级为待验证，不作为确定性结论交付。
