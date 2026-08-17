# Android 知识库与证据规则

## 来源优先级

1. 当前项目源码、Manifest、Gradle 配置和构建结果。
2. 与 API level、target SDK 和设备版本匹配的 Google Android 官方文档。
3. 固定版本的 AOSP、AndroidX、SDK source 和 CTS/CDD。
4. 小米 HyperOS/MIUI 官方适配文档；仅用于小米特有行为和兼容性。
5. GitHub 开源仓库；用于算法、数据结构和实现方式对比，属于非官方证据。
6. 团队经验；只能作为辅助线索，不能替代官方依据。

## 编码前检查

- 先调用 `android_environment(action="detect")` 和 `android_project(action="discover")`。
- 先用 `android_kb` 检索项目已有实现，再检索官方来源。
- 算法、数据结构和实现对比可用 `android_kb(action="github_search", scope="github")`，但必须保留仓库、提交、许可证和内容哈希。
- API、权限、Manifest、Gradle 依赖、生命周期、后台任务和厂商兼容性变更必须保留 `evidence_id`。
- 查询结果为空或只有未经验证的经验时，返回 `evidence_insufficient`，不得凭猜测生成确定性实现。

## 写入规则

- Kotlin、Java、Gradle、XML 和 Manifest 必须通过 `android_file` 修改。
- 现有文件每个 edit 必须提供 `old_content`。
- 先使用 `dry_run=true` 检查 diff，再落盘，落盘后重新读取。
- `android_file` 写入必须带有效的 `evidence_ids`；纯格式化可以声明 `change_type="format"` 免除官方引用。
- 涉及小米行为时必须提供 Xiaomi 官方来源和目标设备/HyperOS 版本。
- GitHub 证据不能满足 API、权限、Manifest、后台、设备或 OEM 兼容性变更的官方证据要求。

## 证据字段

每个证据必须保留来源 ID、标题、URL、定位信息、版本、内容 hash 和抓取时间。GitHub 证据还应保留 repository、ref、commit 和 license_ref。官方网页只能作为带 URL 的受控缓存，不得把未经许可的整站内容重新发布。
