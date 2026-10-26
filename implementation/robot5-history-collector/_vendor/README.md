# Markdown 转换依赖

随采集模块提供 Mistune 3.1.3（来自 PyPI 官方发行包，BSD 3-Clause 许可证）。

许可证见 [MISTUNE_LICENSE.md](MISTUNE_LICENSE.md)。将此目录与采集模块一起迁移，无需联网安装。`answer_format.py` 使用转义 HTML 的渲染器和表格插件；此处保留上游源码，不修改库行为。

重新获取相同版本：`python3 -m pip install --target <空目录> mistune==3.1.3`。
