# 日常运行、检查与验收

## 服务和任务

平台7容器：`WeKnora-frontend`、`WeKnora-app`、`WeKnora-docreader`、`WeKnora-postgres`、`WeKnora-redis`、`WeKnora-neo4j`、`WeKnora-bailian-asr`。

旧机5个LaunchAgent：

| 名称 | 计划 |
|---|---|
| `com.xinchengyuan.weknora-project-consultation-sync` | 常驻，同步扫描60秒，稳定等待180秒 |
| `com.xinchengyuan.robot5-history-collector` | 60秒，采集与CRM同进程 |
| `com.xinchengyuan.wedrive-test-compression` | 北京时间03:00，实际范围为售前文档 |
| `com.xinchengyuan.wedrive-test-compression-recovery` | 120秒 |
| `com.xinchengyuan.wedrive-test-compression-alerts` | 300秒 |

周期任务在两轮之间显示 not running 正常；同时核对最近退出码、health时间和实际输出。日志、解析、网页排版、Excel导出不是额外的常驻LaunchAgent。

## 数据位置

- 旧机来源：`/Users/xinchengyuan/Library/Containers/com.tencent.WeWorkMac/Data/WeDrive/海那边/项目to咨询`。
- 旧机Markdown：`/Users/xinchengyuan/Desktop/当前解析结果/项目to咨询/Markdown`。
- 同步配置和状态：原项目 `.local/weknora-sync/config.json`、`.local/weknora-sync/live/`。
- 问答档案：原项目 `outputs/机器人5历史问答采集/history.sqlite3`。
- 压缩台账：原项目 `outputs/售前文档定时压缩_20261019/registry/`。

## 当前基线的边界

2026-10-26上午的只读核对：7容器运行，5任务已加载；同步存在7个解析暂停项；809组问答已采集，CRM台账806条符合条件的记录为synced。这是整理前的观察基线，不代表本次发布目录已部署，也不表示本次逐条重新回读CRM。

Agent绑定「售前文档·企微智能机器人」，WebSocket，smart-reasoning，主回答模型deepseek-v4.1-flash；知识工具为search_knowledge/read_document/list_documents。Keenable供应商已配置，但Agent的web_search_enabled=false。Neo4j已运行，真实问答参与情况未在本轮核验。

现有删除规则依赖本地连续缺失3次且至少60秒；大量缺失阈值保护关闭，不能等同独立云端删除证据。压缩使用client_sync验收，不能等同独立云端下载核验。

## 故障处理

先读health、当前问题清单和单文件trace，再看任务报告；不要清空state、映射、预算或CRM去重信息来解决故障。新版本解析失败时保留旧活动版本；上传结果未知时先回查，避免重复写入。解析中health暂旧应结合请求收据与日志判断，不能仅凭时间旧强杀进程。

## 接管前的真实验收

分别记录新增、修改、改名、受控删除、解析失败留旧版、索引、检索、真实企微问答与引用、采集、CRM写入读回。人工评分由人填写，系统同步成功不等于人工评测通过。测试文件放在专用目录，避免把虚构资料留在业务知识库。

更完整的历史运行手册见 [既有部署运维手册](../implementation/weknora-sync/部署运维手册.md)。其中历史时间、数量、包版本和开关不可替代当前检查。
