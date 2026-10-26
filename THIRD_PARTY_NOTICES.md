# 第三方代码与许可

## WeKnora

- 上游：https://github.com/Tencent/WeKnora
- 本次源码基线：`3e8b0bfc80b845b2d4b2ed683994748741450a97`，本地标签 `v0.8.2`。
- `platform/weknora/` 保留原有 [LICENSE](platform/weknora/LICENSE)、[第三方声明](platform/weknora/THIRD_PARTY_NOTICES.md)、`licenses/` 及子组件许可。
- 当前本地 tracked 修改保存在 `patches/weknora-tracked-changes.patch`；新建 Go 文件已收录在源码树中，不包含在该 tracked-only 补丁里。完整源码树是本次归档依据。
- 适配器依照来源保留目录与说明，不把第三方代码统一改署为自有代码。

## Mistune

问答模块内置 Mistune 3.1.3，采用 BSD-3-Clause。许可证位于 `implementation/robot5-history-collector/_vendor/mistune-3.1.3.dist-info/LICENSE`。内置版本用于确定性 Markdown → HTML，不需要在运行时另行下载。

## 自建代码与运行依赖

当前整理目标是个人内部保存，未替权利人决定将自建代码开放许可，因此根目录不擅自套用 MIT 等开源许可证。公开分发前另行确定自建代码授权范围。第三方部分继续适用各自许可证。

Python 依赖通过 `requirements.txt` 安装；未捆绑 Python、Node、Docker 镜像、LibreOffice、Poppler 或 Codex artifact 运行时。对应软件许可继续适用。
