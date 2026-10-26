# 机器人5：采集端与服务器副本

现有 `worker.py` 继续负责解析、映射、上传去重、索引验证、新版本激活和旧版备份清理。默认 `source_mode=local`，既有生产服务保持此模式。本地隔离验收不连接生产知识库，不调用外部模型。

## 组件

| 程序 | 职责 |
|---|---|
| `collect_replica.py` | 稳定扫描、原件快照、持久待传批次；只读采集来源 |
| `replica_transport.py` | 本地测试通道和 OpenSSH SFTP 适配；先载荷、后 READY，获取 ACK |
| `receive_replica.py` | 指纹验证、顺序提交、原件版本备份、删除凭据及崩溃恢复 |
| `worker.py` 的 replica 模式 | 只处理已提交副本；缺少有效凭据时不传播服务器目录缺失 |
| `migrate_replica.py` | 保留来源 ID、知识 ID、共享关系和所有权；校验资产并重绑定环境 |
| `verify_replica.py` | 创建全新合成目录，贯通迁移、增改名删、故障及恢复验证 |

## 批次与确认

每个批次由固定 `scope_id`、固定 `generation`、递增 `seq` 和清单 SHA256 唯一标识。重启不更换 generation。采集端同时只保留一批未确认数据，整段发布受 publisher.lock 保护。

`objects/<SHA256>` 保存不可变原件快照，`manifest.json` 记录相对路径、指纹、大小及删除证据，最后写 `READY.json`。未 READY 的目录不进入解析。服务器校验后与 worker 共用 `worker.lock`，先应用全部新增/修改，再应用删除，写接收账本后返回 ACK。

ACK 只证明服务器来源副本已持久提交，**不证明 Markdown、索引或机器人回答完成**。ACK 丢失可重取；同号不同清单、跨范围/跨 generation、缺号批次、坏指纹、路径逃逸和符号链接均拒绝。NFC 规范化后重名暂停。采集目录扫描读失败不形成删除证据。

接收提交日志 `commit.json` 存在时，worker 保护暂停。重启接收器先恢复该批，再允许扫描。接收器独立维护账本，不修改 worker 的活跃映射。配置中的 worker 状态目录与接收器记录不一致时拒绝运行。

## 删除边界

采集端保持连续至少3次完整缺失扫描、至少60秒；根目录更换、占位文件和大批缺失保护暂停。可唯一识别的同内容改名合并提交，复用原件和知识映射。

服务器只消费本范围、本 generation、已提交序号的删除凭据。删除指纹与最新接收原件比较，不与可能落后的已激活版本比较。采集端已等待，服务器不重复等待。原件副本先备份；worker 继续检查共享引用、备份 Markdown、清理 active/pending 条目并验证远端404。源重新出现使旧凭据失效；旧批次重放不会删除新文件。

这些仍是本地目录缺失证据，不能保证区分所有取消权限/同步与真实云端删除；少量或分批缺失可能仍被判断为删除。云端证据核验另行优化。生产缺失规则在本轮未切换。

## 命令与配置

`examples/replica/` 提供采集、接收、传输和 worker 模板。先填写绝对目录、固定 scope/generation，三端一致。接收原件、incoming、接收账本、worker状态、Markdown使用各自目录。SSH专用账户只授权 incoming 及读取 ACK；目标源文件与状态由服务账户维护。不要把生产凭据放入模板或验收包。

```sh
python3 implementation/weknora-sync/receive_replica.py --config receiver.json --watch
python3 implementation/weknora-sync/collect_replica.py --config collector.json --transport-config transport.json --watch
```

OpenSSH SFTP 使用密钥、`BatchMode=yes`、`StrictHostKeyChecking=yes` 和专用 known_hosts，上传使用 `put -f` 请求 fsync。首次应预置可信主机公钥，不能自动接受未知主机。未发布批次可重传完整对象；当前不是按字节偏移续传。协议依据：[OpenSSH SFTP 手册](https://man.openbsd.org/sftp)。本地真实 SFTP 验证通过 `sftp -D sftp-server`，不涉及外部SSH认证或真实网络。

## 受控迁移

1. 先完成服务器全量 baseline，正式 worker 暂不启动；保持删除开关关闭。
2. 旧 worker 待处理上传收敛，停止旧实例，保存最终状态、Markdown、解析资产与 WeKnora 数据。知识库和知识ID保留。
3. 用服务器读出的知识清单校验ID、文件指纹、大小和目录范围，再运行迁移。

```sh
python3 implementation/weknora-sync/migrate_replica.py --source-config old-config.json --target-config server-worker.json --inventory server-knowledge-inventory.json --source-stopped
```

在服务器使用导出的资产时，另外传 `--state-snapshot`、`--markdown-snapshot`；旧绝对路径用于识别，新路径用于实际写入。必须先停止旧 writer；单周期文件锁不能替代停机切换。该工具不修改旧状态，不连接平台，不接受重建后的不同KB ID。pending不明确的上传须先核对。

迁移校验当前来源副本与映射指纹，校验 Markdown 与平台清单，复制 artifacts/history/retired-backups/jobs，保留全部来源ID、remote_ids、owned_ids和retire；只改运行路径及身份绑定，清空旧文件属性缓存与缺失计时。工具保留原排除项及解析模型规则，输出 `effective-target-config.json`，正式 worker 使用该配置，避免模板遗漏排除项。历史解析任务作为证据保留，不强制跨平台resume。迁移中断标记存在时 worker不执行；状态已提交但完成标记未移除时可幂等恢复。

4. Linux Office转换隔离机制、中文字体及工具路径适配并验证后，启动一个服务器 worker，补传切换期间的增量。全量对账及真实问答验收后，启用 `automatic_deletion_enabled=true`。现有解析仍使用macOS的 sandbox-exec；本轮未将其冒充Linux可运行。

## 隔离验收

```sh
PYTHONPATH=implementation/weknora-sync python3 -m unittest discover -s implementation/weknora-sync -p 'test_*.py' -q
python3 implementation/weknora-sync/verify_replica.py --output outputs/新的隔离验收目录
```

验证模拟知识库与合成原件。保留输出路径的配置、最终映射、来源历史及 `验收结果.json`。真实服务器SSH认证、网络、持久卷迁移、Linux解析与机器人回答留到部署联调；不能用本地通过替代它们。
