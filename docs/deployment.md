# 部署文档

本次只整理源码，没有启动第二套平台、加载LaunchAgent、调用模型、写CRM或停止旧服务。本目录可以独立阅读和离线验证，但不包含生产数据库和私有备份；全新安装与迁移接管必须分开。

## 1. 环境和依赖

- 主运行形态：macOS用户会话、企业微信客户端微盘同步、Docker Desktop。
- Python建议3.12；依赖见根目录 `requirements.txt`。macOS `sandbox-exec`、LibreOffice、Poppler用于相应文档转换，OpenSSL用于原加密迁移工具。
- 本地OCR是可选依赖，不随代码提供；模板默认禁用，需自行准备模型和rapidocr环境后再配置。
- 平台从当前收录源码构建，构建工具链与镜像依赖由其Dockerfile定义，需要联网拉取。GitHub源码保存不包含镜像。
- Compose需支持 `!override` 标签。旧机仍运行同名容器，禁止在旧机直接执行下面的新部署 `up` 命令，避免同名容器和端口冲突。

在新环境进入发布目录根部：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/check_release.py
.venv/bin/python scripts/test_release.py
```

原artifact Excel后端依赖Codex Node与artifact模块，本目录不复制node_modules。独立安装统一选openpyxl；原有两个artifact专项测试在依赖不可用时明确跳过。

## 2. 新环境：启动空平台

仅在准备部署的新机器执行。以下会创建一套空平台，不恢复旧知识库或企微会话。

```sh
cp config/platform.env.example platform/weknora/.env
```

编辑 `.env`，将 `DB_PASSWORD`、`JWT_SECRET`、`NEO4J_PASSWORD` 改为本机生成的独立随机值；保留并安全备份该文件。填写其他平台要求的配置，不将该文件加入Git。示例值只是占位符。

```sh
chmod 600 platform/weknora/.env
docker compose --project-directory platform/weknora -p robot5-release \
  -f platform/weknora/docker-compose.yml -f config/docker-compose.release.yml \
  --profile neo4j config --quiet

docker compose --project-directory platform/weknora -p robot5-release \
  -f platform/weknora/docker-compose.yml -f config/docker-compose.release.yml \
  --profile neo4j build app docreader

docker compose --project-directory platform/weknora -p robot5-release \
  -f platform/weknora/docker-compose.yml -f config/docker-compose.release.yml \
  --profile neo4j up -d frontend app docreader postgres redis neo4j bailian-asr
```

确认7个服务状态、`http://127.0.0.1:8091/health`和网页 `http://localhost:8090`。本次整理只验证Compose配置，不执行镜像构建或上述启动命令；新环境构建、平台登录及真实调用仍需部署时验收。

`config/reference/docker-compose.current.yml`只是原机挂载参考，包含原机二进制路径与私有业务表格挂载，不能替代release覆盖文件。release覆盖文件改为从源码构建app，且不启用未收录的业务CSV兼容清单。

## 3. 新环境：模型、知识库与企微渠道

通过管理页配置聊天、Embedding、Rerank、视觉和ASR模型，填入本地私有凭据。源代码不含已配置模型和Agent数据库记录。

创建知识库，保存真实知识库ID。创建Agent并限定知识库；旧机参考为smart-reasoning，主回答deepseek-v4.1-flash，工具search_knowledge/read_document/list_documents，最大10轮。模型别名和供应商可用性以账号实际情况为准。

企微渠道采用API模式WebSocket长连接，BotID/BotSecret只在私有配置中填写。先保持正式渠道关闭，完成平台检索与回答验证后再启用专用测试渠道。不要在两台电脑同时连接同一个正式机器人。当前旧机Agent联网搜索关闭；不要仅因为Keenable供应商已配置就启用它。

## 4. 新环境：绑定同步

登录企业微信并实际下载来源资料。配置来源必须是本机真正落地的目录，不能照搬旧账号缓存和占位文件。

复制 `config/sync.example.json` 到独立运行目录的 `sync-config.json`；运行目录应在Git发布目录之外，原机生产 `.local` 不用于这次新部署。逐项替换：

| 占位 | 内容 |
|---|---|
| `@SOURCE_ROOT@` | 新机实际微盘来源目录 |
| `@RUNTIME_ROOT@` | 新机独立运行目录，使用绝对路径 |
| `@RELEASE_ROOT@` | 这个源码目录的绝对路径 |
| `@KB_ID@` | 新平台创建的真实知识库ID |
| `@PYTHON@` | 本虚拟环境Python绝对路径 |
| `@SOFFICE@` / `@PDFTOPPM@` | 新机安装的转换和渲染可执行文件绝对路径 |

在运行目录创建private、sync、markdown目录，将知识库API Key单独存入 `private/api-key.txt`；按 `config/model-env.example.json` 格式建立 `private/model-env.json`。私有目录700、文件600。不得把真实密钥写回example文件。

**只有新知识库**才将 `config/baseline.empty.example.json` 复制为运行目录 `baseline.json`。旧库迁移不能用空baseline重建，必须恢复原映射、旧知识ID及状态。配置模板默认自动删除和模型调用关闭，大量缺失保护打开；这是新安装模板，与旧机配置参考有意区分。

先执行不启动writer的状态命令：

```sh
.venv/bin/python implementation/weknora-sync/worker.py status --config /实际运行目录/sync-config.json
```

路径、权限、API和专用测试来源核对后，将 `allow_model_calls` 设true并记录既有授权，配置有效的 `model_authorization`，再手动运行一轮：

```sh
.venv/bin/python implementation/weknora-sync/worker.py run --config /实际运行目录/sync-config.json
```

`run`会实际调用外部服务和写知识库。完成新增/修改/改名、解析异常留旧版、索引检索与真实企微回答验收后，再用 `watch` 常驻运行。`watch_enabled`并非一个可依赖的总停机开关；真正的启动/停止由进程和LaunchAgent控制。

## 5. 新环境：问答采集与CRM

复制 `config/history-binding.example.json`，将channel_id和agent_id替换为新平台真实值。`postgres_container`使用新平台容器名，`export_backend=openpyxl`、`export_async=true`。

在独立档案目录执行：

```sh
.venv/bin/python implementation/robot5-history-collector/collector.py sync \
  --root /实际运行目录/history --binding /实际运行目录/history-binding.json \
  --export-backend openpyxl
```

首次采集只读查询WeKnora，创建本地SQLite；无需CRM配置也能采集。要启用CRM，将 `config/crm.example.json` 复制到档案目录 `private/crm-config.json`，填写真实app_key/app_sign、工作表与视图，保持enabled=false先检查结构和权限；试点与验收流程见 [CRM同步说明](../implementation/robot5-history-collector/CRM同步说明.md)。`crm-sync`是可强制写入的试点命令，不能当只读健康检查使用。完成真实写入回读后再开启enabled。

旧机默认artifact后端保留，但独立安装必须通过binding选择openpyxl。原 `install_service.py`硬编码默认参数，不直接复用；应以这里核对过的绝对路径、root、binding和Python解释器创建60秒用户级任务。

## 6. 压缩与后台调度

模板 `config/compression.example.json` 默认apply_enabled=false。替换SOURCE_ROOT、RUNTIME_ROOT、CLIENT_DATABASE；核对专用企微账号的客户端数据库、空间身份与编辑权限。仅授权的测试/售前文档范围受支持。备份台账必须在来源目录外。

先准备候选与备份，检查原件、候选、哈希、文件身份和锁协议后，再启用回写。正式计划为03:00压缩、120秒恢复、300秒异常检查。异常检查模板的registry与state_dir必须绑定新机目录。

随源码保留的plist是旧机定义：解释器和路径都要换，且相同Label会与旧机任务冲突。只有在新机完成前台运行验收后，才安装5个用户级LaunchAgent，设置Asia/Shanghai；不要直接复制旧机plist加载。用户登录、Docker启动、企微登录和微盘同步正常是运行前提。

## 7. 现有生产数据迁移

源码目录不能完成生产接管。正式迁移沿用 [既有Mac迁移说明](../implementation/weknora-sync/README_mac_deploy.md) 和 [部署运维手册](../implementation/weknora-sync/部署运维手册.md)：

1. 在原运行项目检查任务收敛、来源与索引，安排最终冻结；本发布目录不充当生产快照。
2. 使用原运行项目及新版打包代码生成加密包，携带平台卷/数据库、知识ID映射、Markdown、解析预算缓存、问答与CRM去重档案。
3. 原打包工具有固定路径并会暂停服务，执行前绑定实际project-root和code-root；本次没有调用，也没有新的最终冻结包。
4. 新机用独立预部署目录恢复，正式企微渠道、CRM写入、自动删除及压缩回写先关闭；核对来源与恢复结果。
5. 停旧机正式writer和渠道，再接管新机，验证实际企微回答、引用、档案、CRM回读和增删改。
6. 回退先停新机，保留接管期间新增数据，再恢复旧机；没有自动合并两端数据库的工具，不执行down -v。

运行密钥、unlock.key和加密生产包应单独保管，不进入GitHub源码目录。发布目录保留内部路径不等于包含这些恢复材料。
