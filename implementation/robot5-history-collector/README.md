# 机器人 5 历史问答增量采集

CRM同步已集成到同一个采集程序。直接从SQLite读取问答，Excel不参与同步。配置、去重与故障恢复方式见 [CRM同步说明](CRM同步说明.md)；运行状态分别查看 `health.json`（采集）和 `crm-health.json`（CRM）。

启用 `export_async` 后，原采集服务触发单个后台Excel任务，采集进程无需等待导出结束。后台任务读取一致的数据库快照后立即释放读事务，只持有 `excel.lock`；采集和CRM继续运行。Excel沿用原生成器、文件名、双页七列结构和全文回读检查。`excel-health.json` 单独记录运行、成功、失败、重试时间及快照版本；采集成功不代表Excel已更新到最新一轮。导出期间的数据变化合并到下一份快照，避免重复启动多个导出进程。迁移快照会等待后台导出退出，保证复制到完整文件。

已部署为本机用户级 LaunchAgent，每 60 秒执行一次，登录后自动启动。数据库、JSON、Excel 均保存在项目的 `outputs/机器人5历史问答采集/`。运行依赖这台 Mac 开机、用户登录和 Docker 中的 WeKnora PostgreSQL 可用；休眠、关机、Docker 停止期间不采集，恢复后按保留的游标补采仍在源库中的记录。

## 数据范围与来源

从 `weknora-local/wecom-connection.json` 读取机器人 5 的渠道 ID 和 Agent ID。只读查询 `messages / sessions / im_channel_sessions / im_channels`，按渠道、租户、企微平台和消息渠道筛选。数据库连接沿用现有本机 PostgreSQL 容器身份，不新建密钥或扩大知识库密钥权限，不修改 WeKnora 源数据库、机器人配置或历史会话。

目前包含机器人 5 企微渠道仍有归属映射的单聊和群聊会话，不含普通网页/API测试会话。已归档、软删除的会话映射也用于补采。历史消息 Agent ID 为空时，标注 `channel_mapping`（渠道归属推定）；存在消息级 Agent ID 时以它为准。渠道绑定变化时停止并报错，避免混入别的机器人。

不能补回源数据库已物理删除、且本地从未采集的内容。`/clear` 等机器人控制指令若未持久化为问答消息，也不在历史问答范围内。

## 配对、增量与去重

- 配对键为同一会话的 `request_id`，不按消息相邻位置猜测问题与答案。缺少请求 ID、缺少问题、多条问题共享请求 ID 均明确标记。
- SQLite `messages.id` 为主键；相同文字在不同请求中分别保留，不用问题内容去重。
- 首次全量补采；以后使用数据库快照时间作为游标，重叠回看 5 分钟，并重新读取未完成回答。每 24 小时全量核对保留数据及映射元数据。
- 查到改变的请求后会同时读取其问题和回答，避免只有半轮记录。每轮在 PostgreSQL `REPEATABLE READ READ ONLY` 快照中执行。
- 原始消息和版本在同一个 SQLite 事务内保存后才推进游标。异常不推进采集游标；Excel 导出失败时保留已采到的数据库内容，下轮重试导出。
- 内容或元数据更新保留版本。软删除保留本地历史并标注；源记录消失不触发本地删除。
- 使用操作系统文件锁避免定时任务、人工补采、导出同时写入。进程退出会释放锁。

## 本地文件

- `history.sqlite3`：完整问题、回答、来源元数据、引用正文、用量及版本；权威本地档案。
- `问答记录.json`：按请求配对的完整问答，便于其他程序读取。
- `机器人5问答.xlsx`：第一张“问答记录”表一组问答一行，保留时间、用户、问题、回答、状态、引用资料六列，2026-10-19 在末尾新增“推理过程”列；第二张“采集明细”表保存记录/会话/请求/消息ID、用量、来源及采集时间等技术字段。数据或导出格式改变后自动刷新。
- `excel-validation.json`：每次发布 Excel 前回读核对两页行数、主表七列、全部问答ID及问题/答案/过程原文，并确认未生成公式。
- `health.json / service.log / service-error.log`：最后采集状态及服务日志。成功状态表示采集和导出成功，不证明机器人回答质量或所有消息送达。
- `验收结果.md / 验收证据.json`：本次补采、去重和真实企微问答验收证据。

2026-10-13 起采用双表格式：主表不再按显示长度拆行，完整问题和回答直接放在各自单元格中；引用资料只展示去重后的资料名称，文档/片段标识放在采集明细。较长问答受 Excel 显示行高限制，选中单元格后可在编辑栏展开读取；显示区域未完全展开不等于原文被截断。

只有原文或过程文本超过 Excel 的单元格容量上限（32,767 个 UTF-16 单位）时，主表单元格明确提示位置，完整内容存入“采集明细”同序号行的连续片段列，两页仍各保持一组问答一行。完整引用正文另外保留在 SQLite 和 JSON 中。公式样式或单引号开头的文字经过文字转义及必要的导出修复，回读核对原文不变。数据库缺失的 token 数据为空，不填 0。

“推理过程”按用户指定命名，实际来源是源数据库 `messages.agent_steps`：按保存顺序展示迭代编号、时间、模型本轮输出、模型实际返回的思考文本（仅有值时）、工具调用/参数/结果/错误等。`thought` 字段实际承载模型本轮普通输出，不能等同于完整内部思考；不会根据最终答案生成或补写推理。源字段为空时标注“源记录未保存执行步骤”；尚无回答或历史记录尚未补采时分别标注。原始 `agent_steps` 与可读 `reasoning_process` 均保存在私有 JSON/SQLite 中，不写入运维日志。

首次接入过程字段使用一次全量只读补采。该操作只为既有消息补充本地归档字段，并保留版本，不重新提问、不修改源库问答。两个导出后端（本机 artifact 与独立迁移版）均支持此列和超长过程全文。

采集明细的“主表序号”对应本次导出主表数据行顺序，稳定去重以记录ID为准。格式变更前的表格保存在 `格式变更前备份/`，历史采集数据库没有改变。

历史回答为 WeKnora 存储的原始正文，可能含引用标签；企微显示会过滤标签并把表格渲染成独立组件，因此正文和界面显示不保证字节相同。按用户要求，当前不做送达验证，Excel 和配对问答 JSON 不生成“企微送达状态”。此前真实企微验收的客户端观察仅作为已发生的验收证据保留。当前重点是原文完整采集、按消息ID去重和自动导出。

## 使用

在项目根目录执行：

```sh
python3 implementation/robot5-history-collector/collector.py status
python3 implementation/robot5-history-collector/collector.py sync
python3 implementation/robot5-history-collector/collector.py sync --full
python3 implementation/robot5-history-collector/collector.py export
python3 -m unittest discover -s implementation/robot5-history-collector -v
```

运行中的服务已经自动更新 Excel，通常直接打开结果文件即可。手动命令返回 `already_running` 时表示后台任务持锁，该次没有执行；稍后重新运行。

服务文件：`~/Library/LaunchAgents/com.xinchengyuan.robot5-history-collector.plist`。

```sh
launchctl print gui/$(id -u)/com.xinchengyuan.robot5-history-collector
# 停止自动采集，保留全部本地数据
launchctl bootout gui/$(id -u)/com.xinchengyuan.robot5-history-collector
# 重新启动
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.xinchengyuan.robot5-history-collector.plist
```

`install_service.py` 可重装相同定义；已有不同定义时会拒绝覆盖。换机器需重新确认 Docker 容器、渠道配置、解释器和表格运行时路径。

代码只使用 Python 标准库采集和存储，Excel 使用 Codex 捆绑的 `@oai/artifact-tool`；依赖目录以 `node_modules` 符号链接关联，不安装或修改包目录。

## 新 Intel Mac 部署适配

2026-10-16起，CLI采集与导出复用同一元数据日志模块，保存到档案目录`logs/`。每轮通过`run_id`关联私有SQLite的`runs`记录，不记录用户问题、回答、引用正文或数据库连接凭据。普通运行记录保留30天；新增/变更、导出与失败事件长期保留。原SQLite、问答JSON/Excel和历史版本不参与日志轮转或清理，日志失败不会阻断采集。

只导出采集元数据，可运行：`python3 implementation/robot5-history-collector/collector.py export-logs --destination outputs/采集排查日志.zip`。使用运行服务的Python解释器；支持包不包含问答档案或原始诊断输出。

新Mac通过部署包的 `mac_deploy.py start-history --home 安装目录` 恢复并启动采集，不使用旧电脑的固定路径或 `install_service.py`。
历史问答、版本、JSON、Excel和验收资料随加密包保存；恢复后的输出在安装目录 `history-archive/`。
新环境的 `binding.json` 固定机器人渠道与Agent身份，并指定本次安装的Compose文件和 `export_backend: openpyxl`。
新Mac使用部署包Python虚拟环境中的openpyxl生成相同双表结构，保留原文回读、超长文本分段和公式防护，不依赖Codex或Node。
首次恢复清空采集游标与全量核对时间，已有消息与版本保留，重新全量读取恢复的数据库后按ID去重，随后每60秒采集一次。
用户级后台任务为 `com.robot5.mac-history-collector`；停止预部署时一并停止，保留档案。
问答采集只读取WeKnora，不修改微盘来源、不发布知识内容、不调用解析模型，不生成企微送达状态。
