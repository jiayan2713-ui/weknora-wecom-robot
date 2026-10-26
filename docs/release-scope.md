# 发布范围与来源

## 已收录

1. `docflow`、同步与问答归档两个实现目录的源码、测试和既有说明。
2. 原工作流脚本、测试和提示词，保持相对目录结构，避免 `PROJECT` 推导和提示词定位失效。
3. WeKnora 当前受 Git 管理的工作区文件，以及新建的 `internal/**/*.go`；包含本地 tracked 源码修改。
4. 选定本地适配器的 Python、Markdown 和 Jieba `.utf8` 数据。上游测试样例和资源属于源码快照，不是生产业务文件。
5. 当前同步、压缩、异常检查配置参考，保留路径、名称、知识库标识；另提供新环境模板。
6. 上游 commit、tracked 修改补丁、逐文件 SHA256 来源清单。

## 未收录

| 内容 | 原因 / 恢复方法 |
|---|---|
| `.local` 私有运行目录、API Key、模型密钥、CRM 凭据、平台 `.env` | 运行时单独放置；源码清单不包含密钥值 |
| PostgreSQL / Neo4j / Redis / 文件卷及 SQLite 问答档案 | 属于运行数据，需独立一致性备份 |
| 微盘原件、业务 Markdown、模型请求响应、真实问答 Excel/JSON | 属于业务数据，不是源码 |
| `local-adapters/enrichment-20261023/tables/` 及 manifest | 包含业务抽取表格；现有表格兼容功能须从私有备份单独恢复 |
| 约250MB的挂载 WeKnora 二进制及历史编译文件 | 超出普通源码发布范围；应从收录源码构建，未宣称与线上二进制逐字节一致 |
| `local-engines/mineru` 环境与相关运行快照 | 本次7容器及自建主链之外的可选解析引擎；如需启用另行安装 |
| node_modules、虚拟环境、缓存、日志、历史部署 ZIP、unlock.key | 依赖重装；运行数据通过原迁移流程保管 |
| OpenViking、机器人1/2/3/4的实现与数据库 | 不属于机器人5当前主链 |
| 上游 `.git` 历史 | 保留 commit 和补丁，无需复制原仓库历史与远端配置 |

## 保留但不能直接执行的参考

- `config/reference/*current*` 是旧机配置参考，不代表新机器已经具备对应路径、权限或状态。
- 原 `build_mac_bundle.py` 会暂停旧机任务并制作私有备份；本次没有运行它。
- 原 `install_service.py` 会安装 LaunchAgent；本次没有运行它。
- 原 plist 任务名会与旧机正式任务相同；不得直接在旧机加载发布目录的第二份任务。
- `patches/weknora-tracked-changes.patch` 用于核对来源；发布目录中的源码已应用这些修改，不能再次叠加补丁。

## 文件清单

`checks/source-manifest.json` 保存复制源文件的绝对路径、SHA256及大小，支持核对原文件与副本；内部名称与路径未脱敏。新增的文档、配置模板和检查工具单独列入 `checks/release-check.json` 的完整发布清单。

本次唯一对复制文件的发布适配是 `platform/weknora/.gitignore`：移除会漏掉已跟踪构建配置的通配隐藏文件规则。原始哈希与发布副本哈希分别记录在来源清单；业务源码未作行为修改。根级忽略规则将运行目录限定在发布根部，避免误排除上游名为runtime/output的源码包。
