# 原始资料解析与评测集生成

本地文件工作流：归档原件 → Qwen解析/模型复核 → 人工确认Markdown → 当前Codex生成候选与必要事实 → 校验并导出XLSX。

## 运行

```sh
./scripts/run_workflow doctor
./scripts/run_workflow status
```

入口优先使用Codex bundled Python；也可设置`WORKFLOW_PYTHON`。独立环境需Python 3.11+与`requirements.txt`中的依赖，Office/PDF转换工具默认使用bundled路径。Office转换使用独立禁宏profile及macOS禁网络沙箱；在Codex限制环境下运行转换可能需要宿主执行授权，程序不会在沙箱不可用时降级为不受限转换。

解析凭据从`DASHSCOPE_API_KEY`环境变量读取。请求文件里只写`api_key_env`名称，不写密钥。默认端点为百炼兼容接口，模型为`qwen3.8-flash`。

### 解析并发与模型复核

新批次默认 `parser_workers: 4`、`model_review: false`，即同一文档最多4个单元并发转写，跳过逐单元模型复核。`parser_workers`可设1至8，跨文档模式下是全批次共享的模型调用上限。`document_workers`默认1，可设1至8；`render_workers`默认1，可设1至4，跨文档共享渲染额度。可用 `parser_workers: 8, document_workers: 4, render_workers: 4` 开启8路流水线。需要模型复核时明确设 `model_review: true`，人工审核步骤不变。

并发任务各自保存请求、响应和重试次数，主线程统一发布检查点，最终正文按原单元顺序组装。观察到失败后不再提交新单元，已在途单元结束后保存成功结果；恢复时复用持久化检查点。并发并不保证服务端吞吐线性提升，仍受限流与转换速度影响。

新批次默认 `empty_body_policy: review`：无文字标记或空正文只保留原响应、原件和待人工确认标记，不重复请求补正文；不据此判定原件确实无文字。需要旧纠正行为时设 `empty_body_policy: recover`。

复核模式、空正文策略及实现参与解析缓存身份，并发数不参与；缺少相关字段的旧批次保持原来的串行、模型复核及正文纠正语义。历史文件哈希相同仅表示候选命中，不能绕过模型、提示词、解析器指纹和缓存完整性检查，不能继承人工审核状态。

### 1. 创建批次

保存`start.json`（示例路径需替换成实际资料）：

```json
{
  "request_id": "project-a-start-001",
  "project": "项目A",
  "sources": ["/absolute/path/to/documents"],
  "urls": {},
  "config": {"id_prefix": "PA"}
}
```

```sh
./scripts/run_workflow start --input start.json
```

数据默认写入项目`workflow-data/`，可用`--root`指定隔离根目录。原件保留不变；音视频及录制辅助文件归档后跳过（包括MP4、M4A、MP3、WAV、MOV、WEMT及索引/播放列表）。多个输入根目录通过共同父路径保留归属。`urls`使用返回的原件逻辑相对名称作为键，没有URL可留空。需要改题量时提供完整整数配置，例如`single_step_questions`、`multi_step_questions`、`out_of_kb_questions`、`ambiguous_questions`与`min_analytical_multistep`；默认32/8/5/5，分析类多步最低4题。

### 2. 审核

返回`awaiting_review`后，阅读并编辑`documents[].working`中的Markdown，对照原件及同版本`.notes.md`记录。保留程序单元标记和来源行；内容错误可直接修改。完成后保存请求：

```json
{
  "request_id": "project-a-approve-001",
  "batch_id": "替换为返回的批次ID",
  "expected_revision": 1,
  "confirmed": true,
  "statement": "用户已审核并确认本批纳入资料",
  "codex_model": "填写本次实际Codex模型"
}
```

`expected_revision`必须使用当前`status --batch <ID>`返回的值。

```sh
./scripts/run_workflow approve --input approve.json
```

明确排除整份文档时增加`exclude: {"d0002": "用户确认的理由"}`；排除某单元使用`exclude_units: {"d0001": {"u0002": "理由"}}`。全部纳入部分均需有审核正文。

明确沿用旧审核版时增加`reuse_review: {"batch_id":"旧批次ID","review_id":"旧审核版本ID"}`以及`reuse_map: {"本批文档ID":"旧版文档ID"}`。程序仅复用条件一致的人工修正版，并保留当前工作稿；出现差异时停止，不自动覆盖。

### 3. 当前Codex生成评测集

读取返回的`generation.prompt`、全部`generation.documents`与`generation.evidence_table`，按提示词写入返回的`submission.candidate_file`和`submission.facts_file`。这是两个内部文件，业务人员无需审核JSON。

候选每题固定：`id/question/answer/evidence/required_facts/category/推理步数`。
事实核对记录每条固定：`question_id/fact_index/fact/sources`，sources每项为`evidence/quote`。

当前评测提示词v1.6.1要求知识库内和模糊题均有非空`required_facts`及逐条原文核对记录；只有知识库外题为空。模糊题记录支持已知部分或澄清依据的资料事实，不把追问动作或未知客户条件填成事实，也不因此直接给出唯一方案。事实优先直接使用完整原文，允许与quote相同，必要整理须保留限定条件。

答案采用销售经理直接解答业务问题的口吻，先说结论，再解释必要依据、条件和计算。出处留在证据及摘录中；影响判断的日期、版本差异和冲突仍须说明。输出前逐题检查口吻和事实完整性，不把“材料费”“补充材料”等业务用语误删。

模板使用说明区的`候选规则：candidate_v3`声明决定校验规则，字段结构仍为7项。历史无声明模板及已有candidate_v2生成任务保留旧规则；修改源模板不会改写批次快照。已有批次需要新规则时，明确使用`regenerate`并在`config`中提供当前`evaluation_prompt`路径，按其已有审核版本创建新的生成任务；不手改历史Manifest或候选。

### 4. 提交与导出

```json
{
  "request_id": "使用submission.request_id",
  "batch_id": "使用batch_id",
  "expected_revision": 2,
  "generation_id": "使用generation.id",
  "candidate_file": "使用submission.candidate_file",
  "facts_file": "使用submission.facts_file"
}
```

```sh
./scripts/run_workflow submit --input submit.json
```

通过后自动生成9列XLSX：在required_facts后增加“审核正文摘录”，由脚本从fact_checks汇总，保留事实编号与出处。相同出处的相同摘录合并显示其对应事实编号，完整明细仍保留。另含来源与逐事实明细、说明，返回托管原版和可编辑副本。历史8列表格仍可核验，不覆盖；文件可读性与往返检查不等于业务审核通过。

### 5. 恢复

```json
{
  "request_id": "project-a-resume-001",
  "batch_id": "使用batch_id",
  "expected_revision": 3,
  "mode": "continue"
}
```

`resume --input resume.json`支持`continue`、`reparse`、`regenerate`、`reexport`。普通继续恢复检查点；重新解析保留旧工作稿并提供新版本，重新出题需要原审核内容仍未变化，重新导出不覆盖已有副本。临时网络错误累计最多3次，候选最多修正2轮。永久HTTP错误修正配置后可明确请求`retry_failed: true`，不重置已用尝试数。

不要删除请求或缓存文件来重试。`status`和`doctor`只读，不自动修复状态。新格式与历史v5/v6交付快照不同；历史JSON需在明确兼容处理后才能作为新候选，不能自动删字段或伪造缺失事实。

## 验证与当前状态

```sh
./scripts/run_workflow doctor
WORKFLOW_PYTHON="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
"$WORKFLOW_PYTHON" -m unittest discover -s tests -t . -v
```

实际完成范围和待审核事项见`docs/design-docs/document-evaluation/reusable-workflow/verification.md`；实施任务见同目录`tasks.md`。项目Skill源文件在`skills/document-evaluation/SKILL.md`。

### TXT解析

支持`.txt`：UTF-8（含BOM）、带BOM的UTF-16，UTF-8解码失败时尝试GB18030。按完整行分段，每段最多200行及12000字符，保留原文件行号；单行超限或文件超过16MiB时要求拆分，解码失败不丢字。空文本仍需人工确认。TXT沿用模型转写、人工审核及评测集生成流程。音视频与录制辅助文件仅归档，不调用模型、不参与出题；按扩展名判定。历史批次不自动改写，新批次使用此规则。

### 空白单元与正文分区恢复

渲染页仅在图像像素全部为白色且没有可提取文本时记录为空白单元；Excel还要求原生单元格列表为空。空单元保留原件、定位和人工审核疑点，不发送模型，不伪造正文。非空页若初稿与正文补全都未能分离出正文，增加一次独立持久化的正文分区纠正，仍失败则停止该单元。普通`resume`复用已完成单元和原始响应，不改写旧记录；新规则仅处理尚未完成的单元。

### 容量与冗余图片链接处理

超过8MiB的渲染PNG按原分辨率分区，保留原页号、像素区域及32像素重叠，不修改归档原件。Excel仅对延伸至XFD、无绘图/链接等附属对象的右侧纯格式空尾部收缩转换范围，保留值、公式和合并区域；其他大表仍需单独处理。带外部图片链接时，只有同一图片节点存在可用的内嵌图片关系才移除转换副本外链；没有内嵌副本仍拒绝转换，绝不访问外链。所有此类转换处理记录仅放入审核疑点，不作为原文出题。

### 解析代码升级后的显式迁移

代码指纹变化时普通continue仍报CONFIG_CHANGED。只有明确授权时使用resume的mode:upgrade_parser：完整缓存验证后保留历史身份并记录接受新版本的映射；部分缓存复制到新版本，保留原准备输入、已完成单元和调用预算，再从断点继续。迁移记录在workflow-data/parser-upgrades/；不修改旧缓存、原件、审核工作稿。已审核批次禁止使用此迁移。

## 2026-09-30 批量解析可靠性更新

正式入口仍是 `scripts/run_workflow`。现有批次、缓存和人工审核稿保留；代码指纹变化后，旧批次需按已有 `resume mode=upgrade_parser` 规则迁移，不应删除缓存或重置请求次数。历史 `outputs/.../自动恢复层` 和补处理脚本是运行快照，新任务使用当前主入口。

新增能力：

- 状态返回每份文件 `quality` 和批次 `quality_summary`，分别显示流程完成、空正文、待确认与审核版本状态；旧批次没有质量统计时明确显示 unknown，不猜测为零。
- HTTP错误正文最多保留64KiB并脱敏，分类为凭据、端点、限流、暂时性、内容拒绝、容量或参数错误。文件级拒绝不会误停整个并发批次；响应无法落盘和完整性异常使用统一停止规则，已发出的请求仍保留结果。
- 大表按文字预算把原始单元格、公式、缓存和合并结构保存为原生补充，视觉页继续保留。默认 `max_unit_text_chars=16000`；这是文字预算，仍保留16MiB请求字节上限。
- PDF非法Unicode文本层保留码点和转义原文，本页改用完整页面图。不能可靠静态化的Office继续报告失败；可以保留静态回退的对象隔离附件并留溯源，转换禁网络、禁宏。
- 默认转换180秒，仅明确超时时再用一次300秒预算；工具缺失与超时分别记录。配置项 `conversion_retry_timeout_seconds` 可调整。
- 内容拒绝可以使用已安装的 `.local/docflow-ocr` 本地OCR；实际OCR子进程禁网络、移除凭据环境。`local_ocr_python=null` 可禁用此兜底。OCR结果始终待人工核对。
- 自动交付稿按内容版本保存。没有人工修改的工作副本可刷新；有人工修改时保留它并发布新的自动稿，避免恢复后仍显示旧残缺稿。友好阅读稿和结构化溯源分开保存。

### 将实际阅读稿导入审核流程

`import-review` 只导入和绑定版本，**不代表审核通过，也不会调用解析模型**。

先按原件编号选择审核资料，生成来源/阅读稿/溯源三个文件的哈希清单：

```sh
scripts/run_workflow doctor
# Python使用本项目已配置的bundled Python，或具有requirements.txt依赖的Python。
python scripts/build_review_import.py --review-dir outputs/4789份解析结果_统一审核 --ids 文件短编号 --output requests/review-import-manifest.json
```

请求示例：

```json
{
  "request_id": "import-reviewed-project-v1",
  "project": "已核对资料版本",
  "manifest": "/绝对路径/requests/review-import-manifest.json"
}
```

执行 `scripts/run_workflow import-review --input requests/import-review.json`，得到新的 `awaiting_review` 批次。之后沿用原 `approve` 操作及用户明确的确认声明/排除项，再进入评测生成；空正文占位符不能用旁注代替正文通过审核。

同一请求重复提交不会创建重复批次；改变阅读稿或溯源需要新请求和新版本。导入后阅读稿再次变化，审批会拒绝旧版本。普通批次的新阅读稿有人工修改时，直接approve会返回 `READING_EDITED`，应先显式导入修改版本，防止误用旧working。

建议按项目选择资料再生成评测；整个4789份的导入清单仅用于盘点和按范围取用，不表示可以将所有资料塞入一次出题上下文。

### 固定样本提速实验

`scripts/parser_pilot.py --manifest <固定样本.json> --output <实验目录>` 默认只显示请求数；显式加 `--execute` 才调用模型。最多6对样本、每路每样本最多1次请求，独立保存请求、响应和usage，绝不覆盖生产缓存。`input_route=text_experiment` 仅作实验，默认继续 `multimodal`。

本次4页实验显示纯文本会漏截图中的付款期限和图表数据，故未提升为默认。数字命中率只做辅助检查，不能证明表格关系、完整性或业务审核通过。

用户已确定默认使用多模态。提速路线比较不作为后续必做步骤；2026-09-30已完成的小样本实验仅保留记录，不继续扩展或提升为默认。原生单元格补充与本地OCR仅用于明确的容量/拒绝降级，不改变主解析路线。
