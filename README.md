# WeKnora 企微机器人5：资料同步、问答归档与运行维护

这是 2026-10-26 从现有运行项目整理的独立 GitHub 源码目录，用于保存和审阅代码、架构与部署方法。仓库：[jiayan2713-ui/weknora-wecom-robot](https://github.com/jiayan2713-ui/weknora-wecom-robot)。整理时的检查结果见检查报告，后续发布状态以仓库 main 分支和 GitHub 发布记录为准。现有业务服务未被此目录替换。

按用户要求，保留内部名称、目录、业务标识和历史路径，不做统一脱敏。生产密钥、业务原件、解析正文、问答档案、数据库和加密部署包不属于本源码目录；运行时单独提供。

## 从这里阅读

- [完整架构图与调用关系](docs/architecture.md)
- [部署文档：新环境安装与现有服务迁移](docs/deployment.md)
- [运维与验收](docs/operations.md)
- [发布范围和未收录项](docs/release-scope.md)
- [第三方许可与上游版本](THIRD_PARTY_NOTICES.md)
- [本次检查结果](checks/检查报告.md)
- [GitHub发布记录与核对方法](docs/github-publication.md)

## 核心组成

| 层 | 内容 |
|---|---|
| 微盘来源 | 企业微信桌面客户端将「项目to咨询」同步到本地 |
| 自建处理 | 同步 worker、docflow 解析、质量门禁、Markdown、版本映射 |
| WeKnora | frontend、app、docreader、PostgreSQL、Redis、Neo4j、百炼 ASR 适配器 |
| 问答渠道 | app 内 WebSocket 渠道，绑定「售前文档·企微智能机器人」 |
| 档案与 CRM | SQLite 增量归档，直接同步明道云，独立导出 JSON/Excel |
| 压缩 | 售前文档每日03:00压缩、120秒恢复、300秒异常检查 |

## 目录

```text
docflow/                              自建解析工作流
implementation/weknora-sync/          同步、解析、压缩、迁移与测试
implementation/robot5-history-collector/  问答、CRM、网页格式与测试
platform/weknora/                     WeKnora 源码与选定适配器
patches/                             上游版本与 tracked 源码差异
config/                              配置示例及旧机配置参考
docs/                                架构、部署、运行与范围说明
scripts/                             原工作流入口和发布检查工具
tests/                               docflow 原有测试
提示词/                              解析与评测提示词
checks/                              清单、哈希及本次检查证据
```

## 本地检查

使用 Python 3.12，并安装依赖：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/check_release.py
.venv/bin/python scripts/test_release.py
.venv/bin/python scripts/check_git_preview.py
```

检查和隔离测试不会启动正式同步、压缩、CRM 或机器人渠道。测试生成文件在系统临时目录。完整测试报告以 `checks/检查报告.md` 为准。

源码没有为了发布而重构，旧脚本仍有本机路径、默认容器名和 Codex 工具链依赖。部署必须按文档绑定路径，不能直接运行旧 plist 或把参考配置当新环境配置。独立部署的 Excel 使用 `openpyxl` 后端；原 artifact 导出实现保留为可选路径，其依赖不随包分发。

本目录不是包含生产数据的迁移包。克隆代码不能恢复已有知识 ID、历史问答、CRM 去重映射或企微登录状态。首次安装和正式接管的区别见部署文档。
