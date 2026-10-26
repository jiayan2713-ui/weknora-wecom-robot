# WeKnora 本地部署

部署日期：2026-09-18。固定官方 v0.8.0，源码提交 `1edcd54b43606d9079bb36650efe3f68707a79ea`。

## 已验证状态

五个基础容器运行正常；应用、PostgreSQL、Docreader 健康检查通过。网页和后端健康接口 HTTP 200；管理员登录成功，系统管理员权限已核验。原有 OpenViking 容器保持运行。

账号为 `admin@weknora.local`，随机密码保存在同目录 `本地管理员凭据.json`（权限 600，已加入 Git 忽略）。已关闭开放注册。

验证结果见 `deployment-verification.json`，镜像摘要见 `deployed-images.txt`。当前尚未配置模型、导入业务资料或接通企微，不代表实际问答质量已验证。

## 地址与数据

- 网页：http://localhost:8090
- 后端健康检查：http://127.0.0.1:8091/health
- 服务只绑定本机回环地址。
- Compose 项目名：`weknora-local`。数据文件、PostgreSQL、Redis 使用独立 Docker 数据卷；不与 OpenViking 共用。
- `.env` 存放随机生成的数据库密码和加密密钥，权限为 600。备份数据时需同时安全备份该文件。
- 默认使用本地文件存储、PostgreSQL 检索；未开启技能执行沙箱、外部追踪或公网入口。

## 日常操作

在本目录执行：

```sh
docker compose up -d
docker compose ps
docker compose logs --tail=80 app
docker compose stop
```

停止服务不会清除数据。不要运行 `docker compose down -v`，该操作会删除本项目数据卷。Mac 重启后需要 Docker Desktop 启动，容器才能自动恢复；休眠或断网期间企微机器人不能持续服务。

## 后续接入企微智能机器人

推荐先使用独立测试机器人，选择企业微信智能机器人的 **API 模式 → 使用长连接**。这是 WeKnora 负责检索和最终回答的接入方式，与普通模式机器人通过 MCP 取资料不同。

1. 配置可用的聊天模型与 Embedding 模型，按需配置 Rerank / 图片理解模型。
2. 导入已核对资料，创建 Agent 并限定知识库，验证本地回答和引用。
3. 在企微创建 API 模式机器人，取得 BotID 和 BotSecret。
4. 在 WeKnora Agent 编辑器的「IM 集成」中添加企业微信渠道，选择 WebSocket，填入凭据并保存。
5. 验证长连接成功，再实际测试企微问答、引用、无答案问题与资料更新；仅有连接日志不能算端到端验收。

文字问答的长连接不要求公网回调地址。图片和文件外链需要企微客户端可访问的 `APP_EXTERNAL_URL` 或对象存储地址，应在正式接入时单独配置。

官方配置依据：`docs/IM集成开发文档.md`（企业微信接入章节）。当前本地部署不等于已接通企微。

## 2026-09-18 知识图谱启用

已启用 Neo4j 2025.10.1 与 APOC，图数据库仅监听 127.0.0.1:7474/7687。账号 neo4j，密码见 .env 的 NEO4J_PASSWORD。主程序已重建加载配置，健康检查通过；土耳其售前知识库已开启 graph_enabled 与实体关系抽取，原有分块、多模态、Wiki 和模型配置保持不变。未重新解析已有文档，未验证真实模型抽取结果。

包含图数据库的启动命令：`docker compose --profile neo4j up -d`。配置和知识库变更前快照在 `local-backups/20260918-165734/`。验证记录见 `graph-deployment-verification.json`。

## 百炼语音适配（2026-09-21）

新增独立 `bailian-asr` 服务，未重编译主应用。语音模型为“百炼语音转写（本地协议适配）”，名称 `qwen-audio-3.0-asr-flash`，Base URL `http://bailian-asr:8080/v1`。已有百炼凭据由WeKnora继续加密管理。9项测试和8秒合成音频真实API测试通过。详见 `local-adapters/bailian-asr/README.md`。上传业务音频需要显式选择ASR模型；知识库默认配置本次未改。

## 2026-10-08 已升级至 v0.8.2

当前主服务、前端和DocReader为v0.8.2，ParadeDB为v0.22.6-pg17。此前v0.8.0说明为历史记录。备份：local-backups/upgrade-v0.8.2-20261008/。验收：verification-artifacts/upgrade-v0.8.2-20261008/升级结果.md。百炼ASR适配器与MinerU保留原版本。
