# GitHub发布记录

目标仓库：https://github.com/jiayan2713-ui/weknora-wecom-robot

可见性：Public，由用户明确选择。

本次提交包含独立发布目录中的源码、配置参考、架构图、部署和运维文档、第三方许可及检查证据。运行密钥、生产数据库、业务原件、历史问答、私有部署包与原始测试日志不在提交中。

`checks/检查报告.md`记录2026-10-26整理完成时的检查结果；其中“尚未创建Git仓库、尚未推送”是当时的状态。后续实际提交与推送结果以Git历史和远端分支为准。

上传后应核对：远端main与本地提交SHA一致；Git树文件数量与对象一致；README、docs/architecture.md、docs/architecture.mmd、docs/deployment.md、docs/operations.md和checks/检查报告.md均存在且内容一致。

架构图由Markdown中的Mermaid代码块提供，另外保存独立.mmd源文件。部署文档区分空平台安装与生产数据迁移；代码仓库不包含生产恢复快照。

## 首批上传

2026-10-26已完成首次推送，main首批提交为`1a63e7cbc2677de7c59ebe1e6d0ed1c77e11789a`，包含4662个文件。随后更新README的发布状态；最新提交以远端main为准。

最终远端核验会对比完整Git树和七份关键文档的回读内容，报告保存于原项目`outputs/GitHub发布核验_20261026.json`。本源码上传没有替换现有业务服务或执行新机部署。

## 网页展示与安全提示核对

GitHub网页未能渲染最初的Mermaid富文本预览，因此补充了可直接显示的`docs/architecture.svg`，作为架构文档主图；Mermaid和Graphviz源文件均保留。SVG由源文件生成，覆盖32个节点和47条调用/数据关系。

GitHub报告1条微信AppID提示，位于`platform/weknora/miniprogram/project.config.json:32`。该文件与上游commit `3e8b0bfc80b845b2d4b2ed683994748741450a97`内容一致，属于随上游源码带入的标识；本次保留提示，没有替用户关闭告警，也不将其当作本机生产密钥。

可选图形重建：临时安装`@viz-js/viz@3.31.0`后，执行`node scripts/render_architecture.mjs /安装目录/node_modules/@viz-js/viz/dist/viz.js`。该依赖只用于文档构建，不是机器人运行依赖。
