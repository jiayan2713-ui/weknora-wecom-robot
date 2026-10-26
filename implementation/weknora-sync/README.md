# 机器人5三方对账与补齐

来源：企业微信微盘本地「项目to咨询」普通文件。原件和机器人1状态只读。

- `reconcile.py`：递归盘点、稳定读取哈希、分页读取 WeKnora，生成来源→Markdown→知识ID映射。内容去重允许多个来源共享知识ID。
- `recover_markdown.py`：复用有来源指纹的完整解析产物，缺失时复制，更新前保留旧副本；不改审核稿。
- `fill_missing.py`：仅上传来源版本已核验、内容缺失的 Markdown；记录上传意图，未知结果不盲重传。409回接已有ID。需要目标库的文档写入授权。
- `align_folders.py`：按精确文档ID调整目录，移动前核对来源、内容和共享引用，保留原归属记录。
- `verify_uploads.py`：等待平台处理完成，读取分块并执行限定文档的关键词检索；不冒充机器人回答验收。
- `parse_large_images.py --prepare-only`：本地原分辨率分区，无网络调用。本轮19份图片已按用户明确授权使用现有 DashScope/qwen3.8-flash 解析；`repair_image_bodies.py` 保留先前响应并修正正文/解析记录分区。
- `recover_recent_sources.py`：复用已完成的机器人1解析产物，来源哈希一致才补齐桌面Markdown，不重复调用模型。

对账示例：

```sh
python3 implementation/weknora-sync/reconcile.py --output outputs/机器人5三方对账_20261010
PYTHONPATH=implementation/weknora-sync python3 -m unittest discover -s implementation/weknora-sync -p 'test_*.py' -v
```

`--reuse-local`复用本轮原件快照并重新读取Markdown和远端，适合本轮补齐回读，不能冒充新的完整原件扫描。远端写入前再次验证来源和Markdown哈希。

机器人5专用凭据存放于项目 `.local/weknora-sync/api-key.txt`，权限0600；不提交、不显示、不复制到交付包。正式读写命令传 `--key-file .local/weknora-sync/api-key.txt`。

静态对账工具不启用常驻同步。持续同步由下述独立 worker 承担；平台 completed 状态不等于机器人问答通过。普通文件范围、占位文件、客户端辅助文件和此前明确跳过的原件分别记录。

## 持续同步

`worker.py` 以指定微盘本地目录为来源，先复用原件哈希已核验的解析结果，缺失或内容变化才按持续授权调用既有 DashScope/qwen3.8-flash。原件与人工审核稿不修改。配置与密钥在 `.local/weknora-sync/`，实时映射在 `live/mapping.json`。

新文件与修改须经过两次稳定扫描；上传意图持久化，网络结果未知时回查内容哈希。新版本完成索引且关键词检索通过后才激活，并备份、清理不再被其他来源引用的旧条目。目录改名优先复用解析。内容去重后多个来源可共享知识ID，映射保留每个来源。

```sh
python3 implementation/weknora-sync/worker.py status --config .local/weknora-sync/config.json
python3 implementation/weknora-sync/worker.py watch --config .local/weknora-sync/config.json
```

后台使用用户 LaunchAgent `com.xinchengyuan.weknora-project-consultation-sync`，登录后运行；日志在 `.local/weknora-sync/watch.stdout.log` 和 `watch.stderr.log`。Mac 开机、用户登录、企业微信客户端及微盘本地同步正常、网络和 WeKnora 可用，是获得新云端资料的前提。worker 不自行判断微盘云端同步完成或登录状态；占位文件不当成原件，来源大批缺失或根目录被替换则保护暂停。

2026-10-12用户要求先实现增删改全自动同步，再优化云端删除核验。当前生产 `automatic_deletion_enabled=true`、`delete_missing_scans=3`、`delete_grace_seconds=60`。微盘删除同步到本地后，该文件连续3次完整扫描缺失且持续至少60秒，自动备份并移除对应Markdown，清理自身管理且无其他来源引用的WeKnora条目。新版本更新仍须索引和检索通过后替换旧版。

当前以微盘本地同步目录状态为执行依据，未接入独立云端删除证据。占位文件、根目录不可用或替换、大批文件缺失仍保护暂停；单个文件重新出现取消缺失计时；改名优先按内容身份恢复映射。目录异常保护不代表所有取消同步场景均能识别，可靠云端删除核验作为后续优化。实际延迟包含客户端同步、每轮扫描和平台清理，不能承诺秒级删除。

`verify_auto_delete.py` 在项目内隔离目录以正式保护期验证自动删除，备份保留，不触碰微盘来源目录、不调用模型。

保留四份既有排除项；无可识别文字和图片资产适配问题保留为 hold，不能标为人工审核通过。

## 超大Excel空白尾部预处理

`compact_xlsx.py` 仅在独立解析副本中流式清理超过32MiB的工作表XML空白尾部，保留正文末行后一行、合并和图片定位范围；超过512行的真实内容或可见范围明确暂停，不扩大通用容量限制。每个非空单元格的类型、属性、公式、缓存及原始文本均核验一致；未修改的ZIP成员逐字节比较。原件快照及其SHA256保留，解析输入副本哈希另记。

`transform_local.py` 在触发Office结构容量保护时使用该预处理，新的请求身份包含副本哈希，保留之前失败批次；解析通过后仍走既有上传、索引及检索验证链。美国L1费用表的详细原因与同步证据位于 `outputs/美国L1费用表超限修复_20261012/`。

## 元数据日志、轮转与查询

`operational_logging.enabled=true`启用文件日志，不增加数据库、后台守护服务或日志平台。沿用`state.json`最近1000条事件作兼容状态投影，关键事件另持久写入`state_dir/logs/audit/`，普通周期状态写入`logs/runtime/`。

记录发现、处理开始、解析/复用、产物准备、上传意图与结果、索引等待与检索验证、映射生效、旧版清理和删除传播。字段统一为北京时间、组件/进程运行ID、来源ID、内容指纹、任务ID、Markdown指纹、知识库/知识ID、相对路径和报告引用。启用时回填仍存的旧事件，且只保存原有已知字段；另写一次明确标注的映射起点快照，分别保存已发布、已观察及pending版本，不把快照当作重新验收。

默认10MiB分段；关键事件满7天gzip压缩但不自动删除。普通结构化日志和后续原始stdout/stderr轮转归档保留30天。首次启用时，旧原始输出校验压缩后进入`private-legacy/`，不自动删除；现有失败历史、jobs、原件、模型响应、旧版备份和问答档案不参与清理。轮转只识别本模块生成的文件名与明确配置的运行输出，不递归清理业务目录。

日志使用字段白名单，不收集正文、问题、回答、鉴权头、配置密钥或任意异常原文。原始诊断输出归入私有归档，支持包不会打包它们，也不包含state、原件、模型请求响应或问答SQLite/JSON/Excel。目录700、文件600。支持包仍有业务文件名和标识，只向授权人员分享。

```sh
# 按文件查看，改名后仍通过来源ID关联；默认最近100条，可再加--source-hash筛选版本
python3 implementation/weknora-sync/worker.py trace --config .local/weknora-sync/config.json --source-relative '咨询skill/咨询SOP-OKR-Task_.xlsx'
# 只导出元数据；目标不能已存在，也不能放回来源目录
python3 implementation/weknora-sync/worker.py export-logs --config .local/weknora-sync/config.json --destination outputs/一次排查日志.zip
```

`watch.stdout.log`启用后只输出简要周期状态，完整状态仍在`health.json`。日志写入失败只报告`operational_logs.logging_error`，不改变解析、上传、重试、映射或删除决策。迁移工具保留日志，并将运行输出路径改写到新Mac；旧预部署包未重制，正式冻结包才包含本次代码及日志。

## 解析完整性与失败处理

### 本机失败提醒与持久日志

在同步配置中启用 `failure_notifications: {"enabled": true, "desktop": true, "desktop_cooldown_seconds": 60}`。
失败记录保存到state目录的 `failure-alerts/`：`当前问题.md`便于阅读，`current.json`供程序读取，按日期的`events-YYYY-MM-DD.jsonl`保留失败、变化、恢复和通知提交历史。日志包含北京时间、相对路径、内容指纹、阶段、错误码、原因、建议、重试状态及解析报告；报告版本匹配时附准备单元覆盖数和未完成定位。不复制鉴权头、任意异常原文或文档正文。

首次启用汇总已有问题一次；同一持续问题按来源ID、内容版本、阶段和错误码去重，正常重启与映射迁移保留去重记录。自动重试首次失败先入日志，连续3次仍失败才弹窗；终止性失败立即进入提醒。全局桌面提交间隔至少60秒，多个待提醒问题合并；通知提交失败退避300秒，失败不会中断同步。恢复只在当前版本active且无pending/重试请求时记录；来源删除或明确排除单独记录关闭，不冒充修复成功。

桌面提醒使用本机osascript，无外部接收人或企微发送。命令返回成功只证明macOS接受提交，横幅仍受通知权限和专注模式控制。服务正常运行时才能检测并发出提醒；电脑关机、服务被强制终止无法自发通知。日志失败单独反映在health的`failure_alerts.logging_error`，不会改变解析、上传或删除决策。

查看当前问题：

```sh
python3 implementation/weknora-sync/worker.py alerts --config .local/weknora-sync/config.json
```

迁移构建与新Mac绑定会保留`failure-alerts/`历史和去重状态。旧的10月13日预部署包未重制，正式迁移需重新生成最终冻结包以带上本功能。

2026-10-12 起，新解析任务在自身 jobs 目录写入 `parse-report.json`，按已准备的页、表、图片分区 ID 检查结果缺漏、重复、额外单元及正文缺失。确证空白单元单独计数；转换处理记录不当原文；疑点保留为警告。不完整正文在上传前暂停，旧活动知识与桌面 Markdown 保留。报告的 `coverage_scope=prepared_units` 仅核验准备单元覆盖，不证明原件页数提取无遗漏或事实正确；`human_review=false`。直接文本及复用产物报告标为 `artifact_only`，不编造页或工作表验收。历史 active 产物不强制重解析。

容量、格式、认证、工具配置、输出截断和完整性错误进入 `parse_blocked_hold`；全部无可辨认文字进入 `no_text_hold`。记录错误代码、分类、恢复建议及报告路径，不把具体原因统一吞成 parse_incomplete。短暂网络故障进入 `parse_retry_wait` 并退避，默认最多3轮失败处理，沿用模型调用本身的实际尝试上限；供应商要求的等待不消耗失败次数，并尊重已记录的 retry_at。源内容变化解除旧版本暂停，解析配置变化需重启服务生效。

修复环境或检查点后可提交单份解析重试：

```sh
python3 implementation/weknora-sync/worker.py retry-parse --config .local/weknora-sync/config.json --source-id SOURCE_ID
```

该命令只写请求，由现有后台 writer 下一轮消费，不启动第二个同步循环、不直接改活跃 state。定向重试建立新任务目录，保留失败证据；可能按既有授权产生新的模型费用，不绕过模型每阶段实际尝试预算。mapping.json 暴露 `parse_failure` 与 `parse_report_path`，health.json 标注解析策略版本及暂停原因计数。此改动不修改同步删除规则；部署升级前检查旧 pending 已收敛，旧 pending 不被冒充新完整性验收通过。

2026-10-15 起，高密度 XLSX 的整表文字超过单元预算时，使用120DPI有重叠像素分区代替整表缩略图，原生单元格、公式、缓存和合并记录继续完整保留。单页最多128分区，模型调用沿用既有并发和尝试预算；正文补救也使用有界并发。分区缺正文仍暂停，未跳过完整性检查。

对于已有不可辨认的整表视图，可通过服务校验入口复用原检查点，保留其他已完成单元：

```sh
python3 implementation/weknora-sync/worker.py retry-parse --config .local/weknora-sync/config.json --source-id SOURCE_ID --checkpoint-report '/实际state目录/jobs/原任务/parse-report.json'
```

入口要求报告位于受管理jobs下，原件指纹、原任务快照及报告指纹一致；请求消费时再次验证。允许在已有活动版本或pending时请求恢复，但不直接覆盖pending：新解析失败时旧版本不变，新产物准备成功后保留旧pending及引用，待新版本索引和检索验证成功才退休旧ID。报告、分区覆盖、补救后的结果及内容指纹另行保存，原缓存及失败报告不修改；不把“复用既有Markdown”当作这次分区修复验收。

## 跨机器副本（本地实现与隔离验收）

采集、SFTP接收、删除凭据、映射迁移及合成验收入口见 [README_replica.md](README_replica.md)。新分支需显式 `source_mode=replica`；现有生产配置保持本地模式。本地隔离验证不代替服务器网络、Linux转换及真实机器人验收。

## 压缩版本联动（默认关闭，测试准备阶段）

`compression_versions.py` 通过显式 `compression_registry` 配置识别压缩回写版本。
台账按来源相对路径和压缩后SHA256定位，另核对source_root、原件备份哈希、校验结果、
微盘文件ID和云端回读版本。压缩来源尚未回读确认时暂停，不把本地写入当成云端覆盖成功。
配置缺失时保持原更新行为；本轮没有修改生产配置或重启服务。

`stage_compression.py` 从已完成渲染和文字比对的压缩产物创建测试候选及带时间戳原件备份。
不修改来源目录、不上传微盘、不调用模型。备份台账必须位于同步来源之外。
`publish()` 是上传适配器契约：适配器必须提供云端inspect、带expected_version的原子替换、
及真实文件download。尚未提供已验证的企微适配器，不能以普通同名上传、删除后重建或本地同步
写入代替其版本检查和云端回读。上传意图先持久化，未知结果仅回查，不盲重传。

已活动原件的压缩版确认后，worker保留Markdown、WeKnora ID及共享引用，更新来源版本哈希。
未入库压缩版由transform_local读取经校验的完整备份，保留原文件名，解析来源哈希与当前微盘
哈希分别记录。员工再次修改则退出压缩复用路径，沿用原有解析、索引、检索验证和旧版退休流程。
隔离测试使用合成WeKnora与微盘传输，不代表真实微盘覆盖或机器人回答已验收。

2026-10-13新增本地同步回写测试：`compression_local_test_sources` 必须列出精确相对路径，
只允许该路径的 `local_write_intent` / `local_replaced` 台账复用已经活动的Markdown与知识ID。
该分支仍核验原件备份、来源哈希、Markdown哈希和远端索引状态，不将本地替换标为云端成功。
未入库资料仍要求云端确认后才能使用原件解析分支。当前仅西班牙非盈利测试副本启用；
待验证企微回写与分享身份后再决定正式范围，不运行全量压缩定时任务。

### 测试目录批量压缩（2026-10-13）

`compression_batch.py` 只接受 `scope_relative=测试`，递归筛选严格大于
30,000,000 字节的普通文件。先将原件以北京时间时间戳命名备份到来源目录外，
保留人员及子目录结构，再从备份生成 DOCX/PPTX/PDF 候选。
`compress_documents.py` 按 85/75/65/55 JPEG 质量逐级尝试，保留图片尺寸；
Office 正文、表格和布局部件逐字节校验，PDF 比对页数、逐页文字和页面尺寸。
机器结构校验与视觉审核分别记录。签名、加密、异常格式或无法达到大小目标的文件不覆盖。

配置及本轮证据：`outputs/测试目录定时压缩_20261013/`。
默认运行仅生成备份和候选，不改同步文件。首份受控试写通过 `write_local()` 执行；
覆盖前复核来源哈希、文件签名和客户端身份，持有 worker 锁与本地文件锁，
保留原 inode、路径和名称。锁不提供云端并发修改的原子版本保护。

新增 `compression_additional_registries` 可挂接批量台账，原单份台账继续保留。
未回读的压缩版本进入 `compression_cloud_hold`，保留旧 Markdown、来源哈希和知识 ID，
不启动解析、上传或退休旧知识；员工改为另一内容哈希后继续走普通更新分支。
对于尚未入库的来源，云端确认后从完整备份解析，不用压缩图像作为解析原件。

独立云端回读确认入口为 `--confirm-receipt`，同时要求 `--downloaded-file`、
`--cloud-file-id` 和 `--cloud-version`。下载副本不得来自同步来源或候选台账目录，
必须与候选 SHA256 一致；调用者应提供实际云端下载和真实上游版本证据。
客户端 SQLite 的 MD5、seq_id 和传输状态只作同步记录，不冒充独立云端回读。

`--apply` 批量模式有启用门槛：配置确认、实际 `cloud_verified` 回读证据，
且没有未解决的写入或回读任务。当前首份云端回读尚未完成，没有启用每天凌晨3点任务。
本轮首份已写入嵌套的西班牙测试副本，其余候选仍未写回。

### 当前启用状态（2026-10-14，覆盖上面的等待回读状态）

用户明确将验收标准改为本地同步覆盖，不再要求独立云端下载。
批量配置 `acceptance_mode=client_sync`，worker 仅对
`compression_client_sync_roots=["测试"]` 接受 `client_verified` 台账。
确认要求源文件 SHA256 等于候选、客户端 MD5/大小一致、文件ID保持一致，
且客户端版本记录已推进；台账明确标记没有独立云端下载，不填写虚构云端版本号。

已完成13份本轮候选的客户端核验（此前试写1份、此次补齐12份），
测试目录剩余大于30,000,000字节的文档为0。已入库的8个知识条目ID、
Markdown哈希、平台内容哈希和更新时间不变，8/8关键词检索通过。
6条既有解析失败保持原状态，压缩不会触发同一原件的再次解析；员工内容更新仍正常处理。
生成答案测试由用户明确取消，未使用管理员凭据。

49项相关回归通过。worker文件锁占用时等待释放后再次核验源文件，
写入结果未知时先确认客户端状态，不盲目重写。员工恢复旧的大文件时创建新时间戳备份，
旧台账保存在receipt-history，原备份不删除。

已通过Codex自动化工具启用“测试目录每日文档压缩”（automation-3），每天北京时间03:00。
只运行固定测试目录的批量命令，未扩大到其他目录，不调用wecom-cli。
无新文件时保持安静，实际处理、失败或需要用户行动时报告。
新验收证据位于 `outputs/测试目录定时压缩_20261013/最终验收_20261014.json`。

### 程序定时（2026-10-14 10:00最终生效）

用户要求程序级定时。已停用Codex heartbeat automation-3，改用macOS launchd，
配置源文件为com.xinchengyuan.wedrive-test-compression.plist，安装到用户Library/LaunchAgents。
每天本机北京时间10:00调用Homebrew Python3.12执行compression_batch.py --apply，
不使用RunAtLoad、不依赖Codex调度。当天10:00自动触发一次，退出0，空扫描且无错误。
详细证据见outputs/测试目录定时压缩_20261013/程序定时配置_20261014.json。

### 退出保护和自动续接（2026-10-15当前生效）

`compression_client.py`只读检查本用户企业微信主进程、WeDrive同步节点、启动稳定时间，
以及配置中的公司和空间缓存身份。进程未运行或缓存身份异常时，候选可以准备，
`write_local()`在回写前及持锁后都会检查客户端，源文件保持不变。
这些条件是回写的必要检查，不是服务器连接或上传成功的证明；完成仍要求客户端MD5、
大小、文件ID和版本记录匹配。

每天10:00日扫描保持原launchd配置。新增
`com.xinchengyuan.wedrive-test-compression-recovery.plist`，安装在用户LaunchAgents，
每120秒执行`compression_batch.py --config ... --apply --resume-only`。
恢复模式只检查已有prepared、local_write_intent、local_replaced、uncertain及
attention_required台账，不遍历源目录；每轮最多3份，每份确认等待默认60秒。
日扫描和恢复共用batch.lock，恢复遇到忙锁快速结束，日扫描有界等待。

恢复先查现有确认。已完成的版本不触发任何文件事件；只有本地内容仍等于候选、
备份完整、文件ID一致且客户端仍记录已知原版本时，允许最多一次mtime事件，
并持久化触发意图，不重写文件内容。确认默认最多3轮，超过上限报告attention_required，
之后仍可接受迟到的客户端确认，但不继续触发。
员工修改或文件身份冲突进入单文件conflict，不覆盖员工版本；其他安全任务继续。

每次结果记录在registry/runs/<run_id>.json，保留last-daily.json、last-recovery.json
和last-run.json，包含起止时间、状态、文件计数、错误及剩余台账数。
waiting和busy是可恢复的等待，不冒充全部同步完成；needs_attention和failed退出2。
配置启动错误也输出带时间的结构化错误。

63项相关测试通过；真实退出客户端、同账号自动登录后，无人工触摸或手动重跑恢复任务，
等待文件自动回写确认，旧离线任务自动触发一次后确认；模拟员工修改保持原哈希且不阻塞
另外两份。8个既有知识条目不变、8/8关键词检索通过，测试文件未入库。
临时文件和排除规则已清理，备份及证据保留。
证据：outputs/测试目录定时压缩_20261013/恢复增强_20261015_093144/最终验收.md。
真实验证范围为同一账号、同一目录的正常退出重开，不代表已验证换账号或清除同步数据。

### PDF PNG 图片支持（2026-10-15）

PDF 压缩现同时处理提取为 PNG 的无蒙版图片与 JPEG 图片，复用图片编码器，
仅在 JPEG 编码更小时替换，不缩小像素尺寸。透明、蒙版图片保留原样；
文本、页面尺寸和全页渲染差异校验仍是覆盖前的必要条件。
新增回归覆盖共享 PNG 图片、透明蒙版保留及既有 JPEG 路径。

### XLSX、失败重试与每日结果（2026-10-15）

支持格式现为 PDF、DOCX、PPTX、XLSX。XLSX 只重编码内嵌栅格图片、
更新必要的图片关系/类型并重压缩 ZIP；工作表、公式、缓存值、样式、图表、
绘图锚点等其他文件内容逐字节保持一致，不通过删除业务数据压缩。
带透明度或蒙版的图片保留；高熵图片遇优化编码缓冲错误时以相同品质、
尺寸和色彩配置改用普通 JPEG 编码。纯数据型大表不保证能低于30MB。
XLS、XLSM、DOC、PPT等未支持格式跳过，不调用格式转换或覆盖；
`.WeDrive`、`.DS_Store` 和 Office 临时文件单独记为元数据跳过。

压缩准备失败按“相对路径 + 原件SHA256 + 压缩模块代码SHA256”记录在
registry/failures。明确的体积/完整性/渲染失败暂停自动重复压缩，
同内容不重复生成备份；内容或压缩算法变化后自动重新尝试。
临时I/O等错误默认24小时后重试，后续间隔加倍，最多3次。
显式传`--retry-failed`可在本轮重试暂停项，原错误、尝试历史和备份仍保留。
这些规则只影响压缩准备；已有同步回执队列继续沿用每120秒恢复和原有次数限制。

每轮完整JSON仍保存在registry/runs。每日结果保存为
registry/daily-reports/YYYY-MM-DD.json及.md，并提供
registry/每日结果_最新.json和.md入口。区分同步成功、无需压缩、格式不支持、
其他跳过、失败或需处理、等待处理，逐文件列出体积、备份和错误/重试时间。
同日多次手动日扫描以最新一轮为每日入口，历史各轮仍保留在runs；
恢复任务仅更新这轮中相同回执的等待项，不把其他文件冒充成功。
每天10:00的现有launchd设置、测试目录范围和备份目录层级保持不变。

### 本机异常提醒（2026-10-15）

新增独立的 `compression_alerts.py` 和
`com.xinchengyuan.wedrive-test-compression-alerts.plist`，每300秒检查回执和
每日任务记录，RunAtLoad启用；不调用压缩器，不扫描或改写业务文件，不依赖Codex。
配置在 `outputs/测试目录定时压缩_20261013/alerts-config.json`。

仅异常时通过 `.local/CompressionAlerts.app` 显示macOS模态弹窗并播放提示音。
弹窗可“定位处理结果”（Finder选中报告）或“稍后查看”；不要求系统通知权限。
正常完成和异常恢复均不提醒。同一异常每天最多提醒一次；异常内容或文件版本
变化可触发新提醒。弹窗实际启动未确认时不消耗去重记录，下轮继续尝试。

- 压缩失败、重试暂停、冲突及执行错误：下次监控检查时提醒。
- 已准备/已覆盖但同步未确认：等待超过15分钟后提醒；未知写入结果或恢复达上限立即提醒。
- 每天10:15起，当天10:00之后仍无实际日扫描启动记录则提醒；恢复轮询不冒充日扫描。
- 日扫描通过 `active-daily.json` 写入真实启动记录；启动超过60分钟无结束记录时提醒。

正常检查状态在 `registry/alerts/health.json`，异常明细在
`registry/alerts/提醒记录_最新.md`，每次弹窗载荷和展示/确认回执保存在
`registry/alerts/notifications`。监控自身的stdout/stderr在输出目录alerts日志中。
电脑关机、休眠或macOS用户未登录时本机提醒也无法运行，恢复后监控会补检。

原生弹窗源代码为 `CompressionAlert.swift`，使用本机Swift编译为AppKit程序，
包标识 `com.xinchengyuan.wedrive-compression-alert`。已安装程序只需编译产物，
日常运行不需要Swift、Codex或网络服务。

### 定时调整（2026-10-16当前生效）

2026-10-19提醒显示调整：按用户要求，alerts-config.json设置desktop=false，
关闭本项目压缩监控的全部弹窗和提示音，保留每300秒的只读检查。
registry/alerts/当前待处理.md和.json每次检查更新，问题变化追加到events-日期.jsonl；
每日结果、备份、压缩、同步及自动恢复逻辑不变。
Robot 5 failure_notifications保持enabled=true、desktop=false，只记录问题日志和清单。

按用户要求，日压缩LaunchAgent改为北京时间每天03:00；
alerts-config.json的daily_hour同步改为3，未启动检查从03:15起生效，
提示文案按配置生成。首次后续计划时间为2026-10-17 03:00。
原每120秒恢复、每300秒异常检查、测试目录范围和原件备份规则保留。

### 人工确认的批量真实删除

当大量缺失保护暂停，且操作人已明确确认这批微盘文件确实删除，可使用 `worker.py confirm-missing-batch --config /实际配置路径/config.json`。该命令只登记当时本地缺失的来源ID、路径、已发布/待处理版本指纹、根目录身份及知识库，24小时有效；记录操作人确认，不声明独立云端核验。文件重新出现会永久撤销其本次确认；新出现的其他缺失仍计入原保护阈值。确认后仍经过既有连续扫描、等待期、Markdown备份、共享引用检查及WeKnora清理流程，不改变全局阈值。确认文件属于当前机器，不随迁移复制生效。

### 暂时关闭数量阈值

配置 `mass_missing_protection_enabled: false` 并重启worker，可关闭“50份与5%取大值”的大量缺失暂停；默认true。此开关不改变根目录身份检查、连续缺失扫描、删除等待、备份及共享引用检查。恢复时改为true并重启；没有自动恢复计时。

### PDF自动兼容修复

`pdf_auto_compatibility_enabled`默认true：新解析时，仅针对pypdf的`Could not read Boolean object`读取错误，生成独立兼容副本；正常文件不重写，加密和其他结构错误不盲修。源文件指纹不变时复用已验证副本；内容变化后按新指纹重新验证。

修复进程由`pdf_compatibility_python`指定，需PyMuPDF；未指定时使用当前Python。新Mac绑定会改为目标安装目录的`.venv/bin/python3`，不沿用旧Mac的ARM路径。默认预算：100MiB、100页、单页1600万渲染像素、120秒。原件单独保留，逐页比较页数、尺寸、108DPI渲染像素及提取文字，全部通过后原子登记到`state/pdf-compatibility/<原件指纹>/`，再继续现有解析、上传、索引与检索验证。

每次修复/复用记录在任务`pdf-compatibility.json`；成功后有`pdf_compatibility_verified`关键事件。失败原因进入parse-report.json、失败日志和已有本机桌面提醒，旧知识版本保留，同一失败版本不会每轮重复调用。超时、缺依赖或验证不一致不会发布副本。提醒提交命令成功不等于系统实际展示，受系统通知设置影响。

### 定向恢复普通文档及图片（2026-10-19）

现有 `retry-parse --checkpoint-report` 也支持 DOCX、PDF、JPG/JPEG、PNG、WebP 的缺正文、单元覆盖不完整和图片资产受阻报告。仍要求受管理任务、当前原件指纹和原任务快照一致；校验缓存与准备单元清单后复用已有正文，只重新读取有真实图片引用或缺正文的单元。正文补救响应可按响应指纹复用；无图的原生文本/批注单元保留其原文，不把文档内指令作为操作执行。恢复结果另存 `source-recovery-results.json`，不修改旧缓存或审核稿。 若定向响应仍生成图片标记，只追加一个独立持久化的格式纠正阶段；失败继续暂停，不无限循环。新检查点恢复会校验并重放前次响应，避免重复调用相同页面。

局部无文字区域须在原模型报告之外再次读取原图，保存图像和响应指纹，才计为 `verified_no_text`；不把照片误标为空白页，也不生成场景描述作为正文。整份文件均无文字仍进入 `no_text_hold`，不上传空正文。报告继续标注 `human_review=false`；准备单元覆盖检查不等同逐字人工审核。Markdown 图片检查区分真正图片目标/已定义图片引用与普通感叹号加文字标记，例如 `EB-5![红字结束]` 不再被误判。

XLSX 的空 `fill` 样式节点会在独立解析副本内补为默认 `patternFill`，保持样式索引不变；除 `xl/styles.xml` 外所有 ZIP 条目逐字节核对不变，并确认副本可读取。原件不改写，修复记录保存在 `xlsx-style-compatibility.json`。其他样式错误不盲目转换。

### 统一自动恢复与持久预算（2026-10-19）

`parse_control.py`统一首次处理、文件更新与检查点恢复；显式检查点只决定从哪里读取已校验产物。处理顺序为独立源结构清单→单元判定→原生文字/已保存响应复用→定向重读→有界分区/格式纠正→再次校验。原件、实际解析输入、压缩前原件分开记录；`source-catalog.json`校对原始PDF页、工作表、非空可见单元格及合并结构，合并非锚点原始载荷仍纳入副本不变校验。`source-coverage.json`及`controlled-results.json`保留来源、区域、原生通道和证据，均不代表人工业务审核。

`automatic_recovery_enabled`默认true，设为false后只评估结果，不执行额外恢复阶段。`parse_limits`仅接受四个正整数：`max_file_requests`默认512（最大10000）、`max_file_units`默认1600（最大10000）、`max_file_seconds`默认1800（最大86400）、`max_recovery_rounds`默认2（最大2）。独立区域继续复用每页128分区的上限；每个网络请求阶段最多3次已发送尝试。文件累计预算和已执行策略保存在`state_dir/parse-runtime/<实际原件SHA>/budget.json`，不会因新job、改名、重启或定向重试清零。

实际输入和模型参数的请求哈希决定共享响应目录；发布任务和来源目录仍独立。旧调用仅在请求身份、响应指纹及来源版本匹配时导入，不修改旧任务。响应命中、模型尝试、累计预算、阶段及单元定位进入现有报告/日志。`MODEL_OUTCOME_UNKNOWN`表示先前发送结果未知，普通恢复禁止重发；先检查已持久响应，必要时由运维明确处理。明确DNS解析/连接拒绝属于`not_sent`，保留独立连接历史并退避，不冒充未知已发送请求，也不消耗三次已发送请求额度；文件总预算仍限制网络恢复。收到但尚未落盘的响应在同一客户端优先使用内存副本，重启后的未知结果不承诺供应商零重复计费。

`run/watch`在加载状态前获取现有writer锁，生命周期内独占；发现来源和启动解析意图先持久化。第二个writer返回2且不会写保护暂停健康状态。需要提前扫描时用`worker.py scan-now --config /实际配置/config.json`向当前writer投请求；不绕过稳定等待或修改删除规则。SIGTERM停止派发文件/单元/网络重试，排空已发请求并落盘；启动器退出宽限为600秒。更新服务必须先停止派发并确认旧writer已释放锁，再更换代码，避免强杀在途请求。常规停止不会清除映射、旧版和请求账本。


### 无文字默认跳过（2026-10-20）

`skip_no_text` 默认 `true`。已有解析链明确返回 `NO_READABLE_TEXT` 后，来源状态为 `no_text_skipped`：保留原件、历史结果和报告，不入库、不作为失败提醒，也不重复解析同一内容版本；原件内容变化后重新判断。此规则不适用于缺页、覆盖不完整、模型请求失败或结果未知。可设置 `skip_no_text: false` 恢复旧的 `no_text_hold` 提醒行为。


### WPS复选框兼容（2026-10-20）

已有明确 true/false 保存值、唯一工作表/绘图锚点及内嵌静态预览的 WPS checkbox，在安全解析副本中移除交互引用、保留图片，并作为原生文字附上逐项状态。原件、工作表数据与合并区域不变。正文缺少或重复任一状态时阻止发布；未知控件、绑定或事件继续拦截。同内容的 `no_text_skipped` 不因解析器升级而自动重试。
