# 10份文档摘要及表格增强修复（2026-10-23）

当前app只读挂载本目录的WeKnora程序及tables目录，保留anydoc/CGO和此前链接/时效补丁。不要删除当前运行产物。

- `WEKNORA_DATA_COMPATIBILITY_MANIFEST=/app/table-compatibility/manifest.json`：仅8个指定知识ID使用缓存值CSV表示，原工作表、源列及行号保留，原件不写入；源文件和CSV SHA-256每次读取均校验。源版本变化会拒绝使用旧表，必须重建兼容副本。
- `WEKNORA_EXTRACTIVE_SUMMARY_KNOWLEDGE_IDS`：仅2个指定文档在供应商明确审核拒绝时采用可见标识的本地原文摘录摘要，不绕过审核或切换模型。
- retry-targets仅供本次固定10条任务维护；已全部成功退出归档，不要再次运行或将计划扩展为全量清理。

详细修复、测试、原件保真、索引检索、同Agent问答、回滚基线见：
`/Users/xinchengyuan/Desktop/原始文件解析_评测集生成/outputs/十份摘要表格增强修复_20261023/修复与验收报告.md`

后续升级需合并本次源码补丁，不能只替换上游镜像后丢掉兼容读取功能。运行回滚可恢复报告rollback目录中的Compose覆盖配置；这不会删除已成功写入的摘要和增强索引。
