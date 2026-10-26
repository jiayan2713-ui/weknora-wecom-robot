# 新Mac独立部署：机器人5

这份包迁移同步程序、已解析Markdown、映射与任务证据、WeKnora数据库/文件/图谱/Redis及原问答设置。本版还包含压缩程序、原件备份及回执、日志、失败提醒和提醒去重状态。企业微信客户端登录资料及本地缓存数据库不迁移；请在新Mac登录专用同步账号。敏感数据在payload.enc中，unlock.key单独传输。不要在聊天中粘贴密钥内容。

## 由采集电脑上的 Agent 执行

未另行发起正式切换任务时，默认任务是新 Intel Mac 的预部署，安装目录默认 `~/Robot5-stage`。已有本机隔离验收不等于目标电脑验收。当前包不能正式接管企微机器人；旧电脑服务继续运行。

用户先将包与匹配的unlock.key分别拷贝到新电脑，并登录专用企业微信账号开启微盘下载。Agent可以自动检查和安装缺失依赖、运行下面的部署命令、注册本用户后台服务并验证网页；登录、扫码和系统需要用户操作的授权由用户完成。只询问无法从文件或环境中确定的路径、权限和安装阻塞，不逐步重复索要同一部署授权。

Agent按以下顺序执行并记录每一步结果：

| 步骤 | 操作 | 成功证据 |
|---|---|---|
| 环境检查 | 确认Intel/macOS及软件支持条件；检查Docker Desktop本地引擎、Compose、Python3.11以上（建议3.12）、OpenSSL3.2以上、Poppler、LibreOffice、sandbox-exec | Docker同时有Client/Server；`docker info --format '{{.Architecture}}'`对应x86_64/amd64；各转换工具可运行 |
| 后台PATH | 本包后台PATH包含 `/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin`；Docker Desktop命令行工具选择系统安装目录 | `command -v docker`为后台PATH可找到的位置；仅在交互终端能找到Docker不算完成 |
| 包检查 | 确認ZIP与单独提供的SHA256文件匹配，解压目录中有mac_deploy.py、manifest.json、payload.enc、app；unlock.key不输出内容 | ZIP指纹匹配；setup随后完成加密负载认证 |
| setup | 在解压目录按下文命令安装 | `install-context.json`中status为installed；目标`.venv/bin/python3`能导入openpyxl |
| restore | 恢复知识库；只操作本包新建的Docker项目，不操作已有项目 | `restore-platform.json`为restored_channels_disabled；knowledge_count与manifest一致；7个服务运行，网页能登录并读取已恢复文档 |
| start-history | 恢复档案、全量去重核对、安装60秒采集任务 | `history-archive/health.json`为success且时间新鲜；档案至少保留manifest对应的包内问答与消息；Excel原文校验通过；间隔60秒以上再查看时间推进 |
| bind | 自动定位或让用户提供专用账号实际来源目录；确认下载完成，再绑定 | `source-check.json`为matched，missing和changed均为空；保持来源ID、知识ID及版本关系 |
| start-sync | 安装同步后台任务 | LaunchAgent运行；`state/health.json`时间持续更新、status为running；检查states及待处理、暂停项 |
| 目标验收 | 网页打开资料，使用已有资料做检索/问答并检查引用；测试后台持续运行 | 报告真实页面/文档/答案证据。网页成功不冒充企微接管或送达成功 |

采集是每60秒执行一次的短任务：两轮之间launchctl显示not running可以正常，需结合last exit code=0和health时间推进判断。同步是常驻任务，应显示running。启动命令成功、计数相同或容器healthy都不能单独替代完整验收。

### 本版新增：压缩、日志和提醒

安装前执行 `xcode-select --install`，确认 `xcrun swiftc --version` 可用。setup会在目标虚拟环境安装PyMuPDF，与现有Pillow一起用于压缩及PDF渲染校验。提醒程序携带Swift源码，在目标Mac本机编译并签名，不复用原Mac的ARM可执行文件。

bind会将全部压缩登记目录迁到安装目录的 `compression/registries/`，重绑定worker配置和压缩回执的来源根目录，保留原件备份、版本证据与历史记录。sync日志与失败提醒恢复到 `state/logs/` 和 `state/failure-alerts/`；问答采集日志随 `history-archive/` 恢复。历史日志中的旧路径保留为证据，不代表新服务仍读取旧路径。

先完成bind，再绑定新专用账号的微盘缓存数据库。数据库通常位于 `~/Library/Containers/com.tencent.WeWorkMac/Data/Library/Application Support/WXDrive/sqlite3/`。可能存在多个账号的库，不能仅凭修改时间选择；命令会只读核对Corp/Room及已有压缩文件身份，不复制原机缓存或登录资料。

```sh
python3.12 mac_deploy.py bind-compression --home "$HOME/Robot5-stage" \
  --client-database '/新账号实际路径/uin..._corpId....sqlite'
```

成功标准：`compression/migration.json`中client_binding_verified=true。企业/空间不匹配、文件身份冲突、定时压缩登记簿含未确认写入或备份损坏时阻塞。旧手工实验登记簿中的未确认回执只保留历史，不交给定时恢复任务重试。旧客户端确认仍属于原始历史证据，不冒充新机独立云端回读。

预部署不执行start-compression：它会回写微盘文件，只有最终冻结包完成activate、旧机停用后才能启用。范围保持「项目to咨询/测试」中超过30MB的PDF/DOCX/PPTX/XLSX。新Mac的系统时区须设为北京时间（Asia/Shanghai）；launchd日历按系统时区触发，仅设置进程TZ不能改变调度时区。每天03:00运行，恢复每120秒，异常检查每300秒；异常通过本机弹窗与声音提醒。

关键日志与普通日志沿用已有策略，不另建日志平台。安装后检查 `state/logs/`、`state/failure-alerts/当前问题.md`、`history-archive/logs/`；压缩每日结果位于 `compression/registries/` 中对应登记簿。问答原文保存在私有档案，不进入脱敏支持包。普通日志保留30天，关键事件长期保留。

### Agent遇到阻塞时

- 来源missing/changed：报告数量和相对路径，区分未下载、权限或快照之后的真实变化；不能用空映射、重置状态、删文件、关闭保护或跳过指纹校验来强行通过。当前预部署快照可能早于最新原件，因此matched不能保证一次成功。
- 包内保留的解析暂停项：列出具体文件与原因，标为未完成，不清除失败记录、不标为人工审核通过。
- 数据库恢复会重建向量索引，大量向量时可能持续数十分钟。容器running或pg_isready成功不等于恢复完成；以restore命令退出码、恢复标记和知识ID核对为准。恢复期间不要反复运行restore。
- 数据库恢复失败或已有restore-platform.json：先只读诊断失败阶段；不反复运行restore，不执行down -v、volume prune，不清空数据库或覆盖现有安装。诊断输出须先去掉凭据。
- 模型解析沿用既有端点、qwen3.8-flash及提示词，只在既有授权资料范围内按需处理；不自行换模型、扩展目录或发起全量重解析。
- 预部署不执行activate，不打开企微渠道，不开启自动删除，不停旧电脑服务。源资料、人工审核稿、历史问答与映射都保留。不要向其他用户发送验收消息；真实企微测试由用户操作或另行明确授权。
- 不输出unlock.key、API Key、模型密钥、完整Compose环境或未经脱敏的数据库诊断。报告只列路径、状态、数量及必要问题，不粘贴历史问答全文。

### Agent交付结果

在目标安装目录保存 `部署验收报告.md`：记录环境版本、包身份、每步命令结果、知识/档案数量、健康文件时间、网页检索与引用证据、暂停项和未验收项。结论明确为“预部署通过”或“阻塞于某步骤”，不能写成“正式接管完成”。正式切换仍按第3节，由旧电脑提供最新冻结包、确认旧服务停止后进行。

可直接给Agent的任务指令：

> 请读取部署包中的开始部署.md，按“由采集电脑上的Agent执行”部分完成本机Intel Mac预部署，默认安装到~/Robot5-stage。自动检查及补齐依赖，依次执行setup、restore、start-history、bind、start-sync，并逐项验证。保留旧电脑服务，新实例企微渠道和自动删除保持关闭，不执行activate。只询问确实无法确定的路径或登录操作，遇到来源版本差异、恢复失败和解析暂停按文档处理。不要输出密钥或历史问答正文。完成后保存部署验收报告.md，明确通过项、阻塞项以及尚未在企微验收的部分。

本版数据库恢复已处理镜像自动初始化扩展与备份同名的问题：仅在安装器刚创建、已检查归属的全新Compose项目内使用pg_restore的clean/if-exists；已有安装或恢复标记仍直接拒绝，不用于重置现有数据库。

## 1. 新电脑准备

- 安装并启动[Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/)，选择对应Apple/Intel芯片的版本。
- 安装企业微信，专用账号加入目标企业并获准读取、下载「项目to咨询」完整资料，开启本地同步。在线文档仍不属于本地普通文件覆盖范围。
- 安装Python3.12、OpenSSL3、Poppler、LibreOffice。若已有Homebrew，可执行：

```sh
brew install python@3.12 openssl@3 poppler
brew install --cask libreoffice
```

依据：[LibreOffice](https://formulae.brew.sh/cask/libreoffice)、[Poppler](https://formulae.brew.sh/formula/poppler)。本包使用macOS转换隔离机制，不是Linux部署包。

将zip解压到新Mac，单独复制unlock.key并执行 `chmod 600 /路径/unlock.key`。以下命令在解压目录执行，`python3.12`也可替换成已安装的Python3.11以上绝对路径。

## 2. 预部署（旧电脑仍服务）

```sh
python3.12 mac_deploy.py setup --key-file /路径/unlock.key --home "$HOME/Robot5-stage"
python3.12 mac_deploy.py restore --home "$HOME/Robot5-stage"
python3.12 mac_deploy.py start-history --home "$HOME/Robot5-stage"
```

解锁先验证认证码，再解密；目标不能是来源Mac或来源Docker引擎。安装目录和Docker项目必须为全新环境，遇到现有数据不会覆盖。镜像固定到来源版本的仓库摘要，在新Mac拉取原生架构。数据库采用[PostgreSQL逻辑备份](https://www.postgresql.org/docs/17/backup-dump.html)，卷按[Docker备份/恢复方式](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes)恢复。

恢复后打开 http://127.0.0.1:8090 ，原管理员账号、知识库和知识ID保留。企业微信问答渠道此时关闭。数据体积较大，请先保证有足够空间容纳镜像、解密资料、恢复后的数据库和微盘原件。

`start-history` 恢复加密包中的历史问答 SQLite、JSON、Excel 和版本档案，首次全量核对来源库，再按消息ID去重，每60秒自动采集与导出。输出位于安装目录 `history-archive/机器人5问答.xlsx`；健康状态见 `history-archive/health.json`。新Mac使用已有Python环境的openpyxl，不依赖Codex或旧电脑Node路径。数据库目标通过本次部署的Compose文件绑定，不沿用旧容器名；渠道及Agent身份保持原值，不采集其他机器人或网页会话。不生成“企微送达状态”。

档案与知识库快照的时间单独记录；首次恢复重置游标做全量核对，源记录缺失也不删除历史档案。预部署可在渠道关闭时核对历史，新增真实问答需正式接管后验收。

在企业微信微盘中对「项目to咨询」选择“在文件夹中查看”，取得新账号实际本地目录；不要照搬旧账号的绝对路径。确认文件真正下载后执行：

```sh
python3.12 mac_deploy.py bind --home "$HOME/Robot5-stage" --source-root '/新账号/实际/项目to咨询目录'
python3.12 mac_deploy.py start-sync --home "$HOME/Robot5-stage"
```

bind计算原件指纹并写source-check.json。缺失或版本差异默认暂停；不能把未下载、权限不足或取消同步认定为删除。绑定后会保留来源/知识ID、共享关系、待处理上传，重新设置根目录身份和运行路径，清空旧文件属性缓存及缺失计时。自动删除保持关闭。

在WeKnora网页验证检索、引用与问答，查看后台日志、state/health.json。新资料处理可能调用既有DashScope/qwen3.8-flash，沿用授权、提示词和计费。原件与人工审核稿保留。

## 3. 正式切换：两台电脑的 Agent 分工

本节是实际切换操作，不是在当前电脑直接执行的安装演示。只有用户在对应电脑明确发起正式切换任务后执行。新机预部署报告须先通过，来源差异、解析暂停及未完成任务已处理；不要等停掉旧机以后才发现这些阻塞。切换期间机器人会有停机窗口，直到新机接管并验收；不承诺无缝切换。

预部署包不能接管机器人。正式切换必须重新导出最终冻结包，迁移最新知识、映射、解析产物及问答档案。两端 Agent 不互相猜测进度，用同一批次的交接文件核对；本说明本身不要求 Agent 给其他聊天发送消息。

### 3.1 切换前：两端准备好再停旧服务

旧电脑 Agent：

1. 在当前项目根目录工作，核对同步与采集的健康时间、待处理任务和暂停项。保留全部来源原件、审核稿、映射与档案。
2. 核对两项 LaunchAgent 是否已加载，WeKnora 服务是否正常。采集是短任务，not running但last exit code=0可以正常；其health时间应持续更新。
3. 只读记录 `WeKnora-app` 的原重启策略，保存到项目私有目录的切换记录，用于回退：`docker inspect --format '{{json .HostConfig.RestartPolicy}}' WeKnora-app`。不输出完整inspect或环境变量。
4. 确认新机报告环境及预部署通过，且已准备足够存储、传输通道和新的空安装目录。不要把原件目录、Markdown和映射删掉来腾空间。

新采集电脑 Agent：

1. 保留预部署报告和 `~/Robot5-stage` 数据；确认最终目录 `~/Robot5` 尚未被其他安装占用。
2. 专用企微账号继续登录，来源目录保持同步；记录实际目录路径。尽量在切换期间避免业务人员修改此范围资料，以便冻结包与新机原件指纹匹配。
3. 暂不打开机器人渠道，暂不开启自动删除。网页问答预部署通过不能替代后面的真实企微验收。

**成功标准：**两端准备报告完整、所有影响接管的暂停/待处理项已解决。尚有6份或其他解析暂停时，先处理，不删除暂停记录、不中断旧服务强行切换。

### 3.2 旧电脑：冻结、打包并保持旧服务停止

在旧电脑项目根目录执行，批次名称必须是新的，以下变量在该终端中保留：

```sh
cd '/Users/xinchengyuan/Desktop/原始文件解析_评测集生成'
ROBOT5_BATCH="机器人5最终切换_$(date +%Y%m%d_%H%M%S)"
ROBOT5_FINAL_DIR="$PWD/outputs/$ROBOT5_BATCH"
ROBOT5_RECOVERY="$PWD/.local/mac-deploy/$ROBOT5_BATCH/resume-old.json"
python3 implementation/weknora-sync/build_mac_bundle.py \
  --output "$ROBOT5_FINAL_DIR" --leave-old-stopped
```

该命令会卸载旧同步、历史采集及三项压缩/监控任务，等待压缩批次和同步写锁释放，停止旧WeKnora写入/问答容器，再导出数据库、文件、映射和最终问答档案。PostgreSQL暂时保持运行用于只读逻辑导出，不代表旧机器人仍在运行。构建失败时脚本会尝试恢复旧服务，必须再实际核对恢复结果，不把失败包交给新机。

**成功标准，全部满足才交接：**

- 命令退出码为0，打印最终ZIP及独立解锁文件路径；ZIP可完整读取且CRC通过。
- 最终manifest中的 `old_services_resumed` 为false；私有 `resume-old.json` 状态为paused。
- `launchctl print "gui/$(id -u)/com.xinchengyuan.weknora-project-consultation-sync"` 和旧采集任务 `com.xinchengyuan.robot5-history-collector` 均查不到已加载任务。仅not running不够，因为定时任务仍可能再次执行。
- `docker ps --format '{{.Names}} {{.Status}}'` 中不再有运行的WeKnora-app、frontend、docreader、redis、neo4j、bailian-asr；WeKnora-postgres仍运行可以正常。其余机器人及无关容器不操作。

为避免旧机重新登录后LaunchAgent自动加载，成功冻结后执行：

```sh
launchctl disable "gui/$(id -u)/com.xinchengyuan.weknora-project-consultation-sync"
launchctl disable "gui/$(id -u)/com.xinchengyuan.robot5-history-collector"
docker update --restart=no WeKnora-app
```

这是对旧服务的可恢复停用，不删除plist、容器、卷或数据。原app重启策略已在3.1记录，回退时恢复。用 `launchctl print-disabled "gui/$(id -u)"` 确认两个标签为true；用上述单字段inspect确认app策略为no。不要手动启动旧app或重新启用旧任务。

给最终ZIP生成SHA256：

```sh
(
  cd "${ROBOT5_FINAL_DIR%/*}" || exit 1
  shasum -a 256 "$ROBOT5_BATCH.zip" > "$ROBOT5_BATCH.sha256"
)
```

旧机Agent保存 `旧机停机交接.md`，内容包括：批次、冻结时间、机器名称、ZIP路径/校验码、独立key路径、恢复日志路径、同步、采集及三项压缩任务已卸载并停用、旧app及其他目标容器已停止、原app重启策略。再附最终包的知识数量/映射数量；问答数量以包内最终档案快照为准，不写死61/122或5252。报告不包含密钥内容。

将最终ZIP、SHA256文件、停机交接报告传给新机，匹配的unlock.key单独传输。**必须使用该最终批次的新key，不能用预部署包的key。**

### 3.3 新机：停止预部署，接收最终批次

先核对用户提供的停机交接报告属于同一最终批次，包含实际停机证据。manifest=false只能证明打包时没有恢复旧服务，不能远程证明旧机此刻仍停止；`--old-stopped`也不会替你远程停旧机。交接证据不清楚时继续保持新渠道关闭，报告缺失证据。

在预部署包解压目录执行：

```sh
python3.12 mac_deploy.py stop-stage --home "$HOME/Robot5-stage"
```

**成功标准：**新机 `com.robot5.mac-sync` 与 `com.robot5.mac-history-collector` 两项任务已卸载；预部署Compose项目的所有服务已停止；8090/8091等原端口已释放。该命令保留预部署数据，不执行down -v或删除卷。

校验收到的最终ZIP：在它所在目录执行 `shasum -a 256 -c '最终批次.sha256'`（替换为真实文件名），应显示OK。解压后进入最终包目录，核对manifest中old_services_resumed=false，key文件设为600权限。发现原来的 `~/Robot5` 已有数据时不要覆盖或清空，先报告其来源。

### 3.4 新机：恢复最终数据并启动两项后台任务

以下命令在**最终包解压目录**执行，不再使用预部署包目录。key及来源路径替换为新机实际路径：

```sh
python3.12 mac_deploy.py setup \
  --key-file '/最终批次unlock.key实际路径' --home "$HOME/Robot5"
python3.12 mac_deploy.py restore --home "$HOME/Robot5"
python3.12 mac_deploy.py start-history --home "$HOME/Robot5"
python3.12 mac_deploy.py bind \
  --home "$HOME/Robot5" --source-root '/专用账号实际的项目to咨询目录'
python3.12 mac_deploy.py start-sync --home "$HOME/Robot5"
```

逐条成功才执行下一条。每一步检查与预部署相同，但安装位置为 `~/Robot5`，并以**最终包**数量和身份为基准：

| 步骤 | 成功标准 |
|---|---|
| setup | install-context.json为installed；最终批次负载认证成功；目标虚拟环境可用 |
| restore | restore-platform.json为restored_channels_disabled；恢复知识数量与最终manifest一致；7服务运行；网页资料能读取 |
| start-history | 最终历史档案恢复、原文校验通过；health为success且最近5分钟内；至少保留最终档案记录，后续只读核对不删除旧档案 |
| bind | source-check.json为matched，missing和changed为空；来源及知识ID保留 |
| start-sync | 常驻任务running；health为running且最近5分钟内；没有pending、queued、waiting_stable、missing_hold、retry_wait、parse_retry_wait或parse_blocked_hold |

如果冻结后原件又变了，bind可能暂停；报告具体差异，与旧机Agent/用户核对，不自行修改来源指纹或绕过对账。进入接管前，新机企微渠道和自动删除仍保持关闭。

### 3.5 新机：正式接管机器人

确认旧机交接证据仍有效、上述最终数据恢复与后台健康检查通过后执行：

```sh
python3.12 mac_deploy.py activate --home "$HOME/Robot5" --old-stopped
```

该命令接受最终冻结包，检查两项后台健康与同步收敛，启用原问答渠道、开启自动删除并重启app/同步任务加载配置。成功提示只代表接管操作执行，下面检查完成才算验收通过：

1. 两项LaunchAgent归属 `~/Robot5`；同步持续running，采集每60秒正常执行；health时间继续更新。
2. 只读检查 `~/Robot5/config.json` 中automatic_deletion_enabled=true；网页/只读数据库确认最终包原启用渠道恢复启用。仅输出状态与ID，不输出完整配置和渠道凭据。
3. 请用户从企业微信向机器人5提一个已知资料问题，确认仅收到预期回复，答案与引用正确。Agent读取该轮会话和引用核对；没有独立证据时不要声称旧机已无连接或消息已送达。
4. 等待至少一轮采集，确认该轮问题、回答原文出现在 `~/Robot5/history-archive/机器人5问答.xlsx`，引用及用户对应正确。再次采集不增加同一消息ID的重复记录。
5. 由用户在微盘新建一个专用验收文件，确认新机原件→Markdown→WeKnora；再修改它验证新版；最后只删除这个测试文件，验证Markdown/WeKnora对应清理及备份/共享引用保护。不要用正式业务文档测试删除。
6. 自动删除仍按既有本地缺失规则工作，微盘客户端传播、稳定扫描、解析与索引都有时间开销；不承诺云端即时生效，也不把取消权限测试冒充已有云端删除核验。

新机Agent保存 `~/Robot5/正式切换验收报告.md`，记录批次、最终恢复数量、两项健康时间、真实企微问答及采集、新增/修改/删除证据和剩余问题。需要用户操作的验收未完成时标为待验收，不提前宣布迁移完成。

正式接管成功后，再启用压缩：

```sh
python3.12 mac_deploy.py bind-compression --home "$HOME/Robot5" \
  --client-database '/新账号实际路径/uin..._corpId....sqlite'
python3.12 mac_deploy.py start-compression --home "$HOME/Robot5"
```

成功标准：三项 `com.robot5.mac-compression`、`com.robot5.mac-compression-recovery`、`com.robot5.mac-compression-alerts` 任务加载，归属当前安装目录；日历03:00、恢复120秒、检查300秒；原件备份和回执字节校验通过。真实压缩回写、微盘确认、下游一致性及实际弹窗仍需在目标电脑用专用测试文件验收，不在预部署时自动改写正式资料。

旧机正式冻结后，除前面的两项任务，还要对旧机三项任务执行launchctl disable，防止重新登录重复处理：`com.xinchengyuan.wedrive-test-compression`、`com.xinchengyuan.wedrive-test-compression-recovery`、`com.xinchengyuan.wedrive-test-compression-alerts`。回退前先stop-stage停止新机全部五项任务；恢复旧机时对应enable，再使用resume-old恢复旧服务。旧机定时配置和全部数据保留。

### 3.6 失败回退：先停新机，再恢复旧机

如果仅安装出错，新机仍未接管也要确认其渠道关闭。若activate中途失败，它可能已经打开部分渠道/自动删除，不能推定它自动回滚。

新机Agent先保存错误及已有数据，执行：

```sh
python3.12 mac_deploy.py stop-stage --home "$HOME/Robot5"
```

stop-stage同样可停止正式安装目录的任务和Compose服务。确认新app及同步/采集已停止并给旧机提供停机证据；保留新机状态、问答档案、产物与数据库卷，期间不重新登录加载任务或启动新app。如果新机无法操作，必须先可靠隔离其机器人连接，再恢复旧机。

旧机Agent收到新机停机证据后，在原项目根目录执行：

```sh
launchctl enable "gui/$(id -u)/com.xinchengyuan.weknora-project-consultation-sync"
launchctl enable "gui/$(id -u)/com.xinchengyuan.robot5-history-collector"
python3 implementation/weknora-sync/build_mac_bundle.py \
  --resume-old '/该最终批次私有目录/resume-old.json'
```

然后根据3.1保存的原策略恢复WeKnora-app的restart设置，例如原值确为unless-stopped时才执行 `docker update --restart=unless-stopped WeKnora-app`；原值是always、no或on-failure时按原记录恢复，不猜测。

**回退成功标准：**旧机目标容器运行、同步任务running、采集health恢复推进，用户企微测试问答正常；新机服务仍停止。恢复日志状态应为resumed，但日志状态本身不能代替这些检查。

回退恢复的是旧机冻结时的环境。新机接管期间产生的会话和产物必须保留并另行对账，当前没有自动合并两端数据库的工具，不能宣称回退会自动保留所有新机新增问答。

### 3.7 两端 Agent 可直接使用的任务指令

给旧电脑Agent（在新机预部署通过、用户决定正式切换时）：

> 按开始部署.md第3节执行旧机正式切换：先检查暂停和待处理项、保存原app重启策略，再生成新的最终冻结包，保持旧同步/采集及WeKnora问答服务停止。核对实际停机，停用旧同步、采集及三项压缩LaunchAgent并将旧app重启策略设为no，生成校验码和旧机停机交接.md。保留全部数据、独立key及恢复日志，不操作无关服务。失败先核对并报告恢复状态，不交付失败包，不输出凭据。

给新采集电脑Agent（收到最终包及旧机停机交接后）：

> 按开始部署.md第3节执行新机正式切换：核对同批次最终包、独立key、校验码和旧机停机证据，停止预部署，在全新~/Robot5恢复最终数据并启动同步与采集。逐步核对原件指纹、知识/档案身份及健康，全部满足后执行activate接管。用户完成真实企微及微盘测试后核对问答入档、新增/修改/删除，保存正式切换验收报告。阻塞不绕过检查、不覆盖已有数据；需要回退时先停止新服务并交接停机证据，不自行恢复旧机。

## 运行要求与边界

新Mac保持开机、唤醒、用户登录；Docker Desktop及企业微信正常运行。登录后同步LaunchAgent自动启动，关机/休眠/退出企业微信会阻断新云端变化。启动或恢复步骤失败时先看错误位置，不反复覆盖恢复数据库；原备份和旧电脑保留可回退。

目前删除仍以本地缺失判断，已有根目录、占位和批量保护，但不能保证识别所有取消权限或分批取消同步；可靠云端删除核验及通用删除撤销另行完善。


### 已登记PDF兼容副本的迁移

`pdf-compatibility`随加密同步状态备份和恢复。绑定时核对原件指纹、副本指纹、页面数及已有逐页校验证据；仅为仍处于`INVALID_DOCUMENT`暂停且来源匹配的PDF生成定向重试请求。不会把旧快照中的暂停记录改成成功，不重试已成功版本，也不将旧副本套用于修改后的原件。


## 2026-10-20 服务清单、日常检查及重启恢复

完整说明见同目录《部署运维手册.md》（在分发包根目录也有副本），包含6类功能/5个后台任务/7个容器、当前配置与日志、压缩权限、问答/CRM、重启身份保护、安全恢复、预部署与正式冻结及新机验收。

ROOT_UNAVAILABLE不能直接当作来源文件被删除。2026-10-20重启后实测目录inode不变、设备号改变；需核对客户端和内容后，正常停止worker并持锁备份，再恢复目录身份、重置缺失等待，保留全部映射和结果。本文不表示已实现自动跨重启重绑。
