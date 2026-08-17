# 证据规范

## evidence_id

每次带有 `require_citation=true` 的 `android_kb` 搜索都会生成 `evidence_id`。写入时通过 `android_file(..., evidence_ids=[...])` 传入；规则引擎会检查证据是否存在、内容哈希是否仍与当前索引一致，以及引用的官方或 GitHub 来源是否仍可复核。结果中的 `authoritative=true` 只表示官方来源，GitHub 结果使用 `source_tier=non_official`。

## GitHub 非官方证据

算法、数据结构、实现方式和测试辅助代码可以调用 `android_kb(action="github_search", scope="github", require_citation=true)`。证据会保留仓库、分支、文件路径、blob SHA、许可证标识、抓取时间和内容哈希；它可以作为实现参考，但不能单独证明 Android API、权限、Manifest、后台限制、设备行为或 OEM 兼容性。

## 必须有官方证据的变更

- Android API 或系统行为
- compileSdk、targetSdk、依赖和插件版本
- Manifest、权限、组件导出和后台限制
- 通知、存储、网络、安全、生命周期和兼容性
- 小米/HyperOS 的通知、后台、自启动、权限、多窗口、大屏或 ROM 差异
- 设备相关行为和发布前兼容性结论

普通业务逻辑可以引用项目源码和测试；算法/实现变更可以引用 GitHub；格式化或纯注释调整可以不带证据。平台契约类证据不足时规则引擎应拒绝写入，并返回重新检索的提示。

## 审计

知识库在项目的 `knowledge/evidence.json` 保存检索证据，在 `knowledge/rule-audit.jsonl` 保存写入校验记录。证据包含来源、标题、URL、定位信息、版本/API level、抓取时间和内容哈希，方便代码审查和问题回溯。
