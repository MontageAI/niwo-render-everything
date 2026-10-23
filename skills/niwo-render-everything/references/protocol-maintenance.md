# 协议维护与发布

字段权威来源是 Niwo 的真实 `BundleSubmitRequest` 模型，不是人工复制的常量表。`bundle-submit.schema.json` 是其离线导出快照，支持无联网、无第三方 Python 包的外部 Agent。

在 Niwo 仓库的 backend 目录运行（路径参数按实际仓库位置替换）：

```bash
.venv/bin/python -m app.schemas.bundle_contract ../frontend/src/utils/bundle-contract.json
.venv/bin/python -m app.schemas.bundle_contract <本Skill目录>/references/bundle-submit.schema.json
.venv/bin/python -m app.schemas.bundle_contract <本Skill目录>/references/bundle-submit.schema.json --check
```

Niwo 的 `test_bundle_contract.py` 阻止模型与已提交快照漂移；前端从同一快照读取上传容量。Skill 的 schema 检查器遇到不支持的验证关键字会失败，不静默跳过。

JSON Schema 负责结构、类型与数值边界；Pydantic 自定义业务校验（段落引用、原文锚点等）、媒体文件检查仍须配套测试，不能声称导出 schema 已覆盖所有业务逻辑。

变更需运行以下检查：

- Niwo：`python -m pytest tests/test_bundle_contract.py tests/test_editorial_bundle_protocol.py`；前端运行 bundleParser 测试。
- 本 Skill：`python -m pytest skills/niwo-render-everything/tests`，覆盖错误→修复→最终ZIP复检。
- 编排端：`python -m pytest tests/test_editorial_bundle_protocol.py tests/test_editorial_asset_pipeline.py tests/test_skill_contract_parity.py`，覆盖真实图片、视频与前景交接，以及相同输入的协议判定。
- 在 Niwo backend 目录用上述 `--check` 命令核对本 Skill 快照。不能只改一个副本后直接发布。

增加字段或素材角色时：先部署编排端，再部署 Niwo 的接口与前端，确认接受新角色后才发布 Skill。v1.2.0 的 `reference_image` 不能提前发给仍只支持 footage/foreground 的服务端。源代码通过测试不代表已经部署。

修改 Skill 必须同时更新 version.json 与 CHANGELOG；发布工作流先跑 Skill 回归测试，再复制到公开仓库。既有用户通过更新检查获取新的校验器和协议快照。
