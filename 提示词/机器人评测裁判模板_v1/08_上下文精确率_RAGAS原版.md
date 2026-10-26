# Context Precision：RAGAS 原版模板（使用标准答案）

整理日期：2026-09-19。固定版本：RAGAS 0.3.1。

**本模板检查每个检索 chunk 是否有助于回答标准答案，并按实际排列顺序计算 Context Precision。** 采用 `LLMContextPrecisionWithReference`，与关键证据召回率、Context Recall、答案忠实度分别计分。

当前状态：已完成原版提示词导出、示例及输入输出核对、无模型的渲染和公式校验。本次未调用裁判模型，未生成机器人4的新分数。

## 1. 怎么使用

1. 准备同一道题的问题、人工审核的标准答案，以及当轮实际用于回答的筛选后 chunk 列表。
2. 保留 chunk 的原始顺序。每个 chunk 单独交给大模型判断，使用相同的问题和标准答案。
3. 大模型对该 chunk 输出 `verdict` 和 `reason`：有用为1，无用为0。
4. 程序按所有 chunk 的原始顺序计算分数，保留逐条判断和理由。

这里的标准答案是 `reference`。不是机器人实际回答，不是 `required_facts`，也不要求单个 chunk 独自支持完整标准答案。指标判断该 chunk 是否提供有用信息，不能据此推断整块文字全部相关。

完整可调用文本见 [context_precision_with_reference.prompt.template.txt](可调用模板/context_precision_with_reference.prompt.template.txt)。该文件保留原版指令、JSON Schema、全部3个原版示例和末尾输入包装。

## 2. 输入格式与映射

RAGAS 接收的单题数据：

```json
{
  "user_input": "原问题",
  "reference": "人工审核后的标准答案",
  "retrieved_contexts": ["第1个chunk的完整正文", "第2个chunk的完整正文"]
}
```

单次模型判断的数据：

```json
{
  "question": "原问题",
  "context": "当前这个chunk的完整正文",
  "answer": "人工审核后的标准答案"
}
```

| 内部字段 | 本项目传什么 |
| --- | --- |
| question | 评测集问题 user_input |
| answer | 评测集标准答案 reference |
| context | 按实际顺序取出的单个 chunk 完整正文 |

源码内部字段名虽然是 `answer`，WithReference实现传入的是 `reference`。不能换成机器人回答，否则变成 WithoutReference口径。chunk编号、题号、顺序、来源及hash由程序保管并关联结果，不把其他题目或评分加入本次输入。

## 3. 原始核心指令

```text
Given question, answer and context verify if the context was useful in arriving at the given answer. Give verdict as "1" if useful and "0" if not with json output.
```

中文释义仅用于理解：给定问题、标准答案和一个context，判断该context是否有助于得到给定答案；有用记1，无用记0，以JSON输出并说明理由。正式原版调用保留英文指令，不用中文释义替换。

只复制这段核心指令并不等于完整原版调用；还需要下面的输出结构、全部示例和数据包装。应通过固定版本RAGAS执行，或使用已导出的完整文本验证集成。

## 4. 单个chunk的输出

```json
{
  "reason": "判定理由",
  "verdict": 1
}
```

字段顺序不影响解析。`verdict=1` 表示有用，`verdict=0` 表示无用。原版Schema的verdict类型是整数；本项目解析时另校验只能为0或1，并要求reason非空。遇到格式错误保留原始响应和错误状态，不把失败当成无用。

模型只判断有用性和理由，程序负责总分。原版输出不强制逐字证据摘录；如以后增加证据摘录字段，应单独标注为扩展模板。

## 5. 原版完整示例

以下3个示例直接从 RAGAS 0.3.1 的 `ContextPrecisionPrompt.examples` 导出，保留原文。它们用于展示原版模型请求，不是本次土耳其题目的评分结果。

```json
[
  {
    "input": {
      "question": "What can you tell me about Albert Einstein?",
      "context": "Albert Einstein (14 March 1879 – 18 April 1955) was a German-born theoretical physicist, widely held to be one of the greatest and most influential scientists of all time. Best known for developing the theory of relativity, he also made important contributions to quantum mechanics, and was thus a central figure in the revolutionary reshaping of the scientific understanding of nature that modern physics accomplished in the first decades of the twentieth century. His mass–energy equivalence formula E = mc2, which arises from relativity theory, has been called 'the world's most famous equation'. He received the 1921 Nobel Prize in Physics 'for his services to theoretical physics, and especially for his discovery of the law of the photoelectric effect', a pivotal step in the development of quantum theory. His work is also known for its influence on the philosophy of science. In a 1999 poll of 130 leading physicists worldwide by the British journal Physics World, Einstein was ranked the greatest physicist of all time. His intellectual achievements and originality have made Einstein synonymous with genius.",
      "answer": "Albert Einstein, born on 14 March 1879, was a German-born theoretical physicist, widely held to be one of the greatest and most influential scientists of all time. He received the 1921 Nobel Prize in Physics for his services to theoretical physics."
    },
    "output": {
      "reason": "The provided context was indeed useful in arriving at the given answer. The context includes key information about Albert Einstein's life and contributions, which are reflected in the answer.",
      "verdict": 1
    }
  },
  {
    "input": {
      "question": "who won 2020 icc world cup?",
      "context": "The 2022 ICC Men's T20 World Cup, held from October 16 to November 13, 2022, in Australia, was the eighth edition of the tournament. Originally scheduled for 2020, it was postponed due to the COVID-19 pandemic. England emerged victorious, defeating Pakistan by five wickets in the final to clinch their second ICC Men's T20 World Cup title.",
      "answer": "England"
    },
    "output": {
      "reason": "the context was useful in clarifying the situation regarding the 2020 ICC World Cup and indicating that England was the winner of the tournament that was intended to be held in 2020 but actually took place in 2022.",
      "verdict": 1
    }
  },
  {
    "input": {
      "question": "What is the tallest mountain in the world?",
      "context": "The Andes is the longest continental mountain range in the world, located in South America. It stretches across seven countries and features many of the highest peaks in the Western Hemisphere. The range is known for its diverse ecosystems, including the high-altitude Andean Plateau and the Amazon rainforest.",
      "answer": "Mount Everest."
    },
    "output": {
      "reason": "the provided context discusses the Andes mountain range, which, while impressive, does not include Mount Everest or directly relate to the question about the world's tallest mountain.",
      "verdict": 0
    }
  }
]
```

## 6. 完整提示词怎样填入数据

完整文本最后只有一个占位符 `{{input_json}}`。用上述QAC结构序列化得到的JSON替换它，正确处理引号、换行和反斜杠。不要直接把未转义的长文本插入JSON字符串，也不要对整段模板使用会解释示例大括号的字符串format操作。

推荐由原版类构造完整提示词：

```python
from ragas.metrics._context_precision import ContextPrecisionPrompt, QAC

prompt_text = ContextPrecisionPrompt().to_string(
    QAC(question=question, context=chunk_text, answer=reference)
)
```

这段代码只渲染提示词，不调用模型。已用普通占位数据和含中文、引号、换行、反斜杠的数据核对：导出模板填入后与 `to_string()` 逐字符一致。

正式评分使用原版指标：

```python
from ragas import SingleTurnSample
from ragas.metrics import LLMContextPrecisionWithReference

sample = SingleTurnSample(
    user_input=question,
    reference=reference,
    retrieved_contexts=ordered_chunk_texts,
)
metric = LLMContextPrecisionWithReference(llm=evaluator_llm)
score = await metric.single_turn_ascore(sample)
```

`evaluator_llm` 需要由评分程序配置，并记录每次模型请求及响应。本模板不新增或更改裁判模型；沿用本轮已确认的设置。多chunk不能合并为单条context，否则改变评分对象和排序含义。实际调用次数受chunk数量、重试和运行配置影响。

## 7. 程序怎样计算分数

令 v_k 为第k个chunk的判断（0或1），K为实际chunk数量：

- P@k = 前k个chunk中有用的数量 / k。
- Context Precision = sum(P@k × v_k) / (sum(v_k) + 1e-10)。

1e-10来自原版实现。保留原始浮点分数，展示时再转为百分比。

| 按原顺序的判断 | 分数约为 | 怎么理解 |
| --- | --- | --- |
| [1, 0, 1] | 83.33% | 两个有用片段位于第1和第3位：(1 + 2/3) / 2 |
| [0, 1] | 50% | 有用片段排第2位，前面有一个无用片段 |
| [1, 0] | 100% | 有用片段在最前面；尾部无用片段不会使此例扣分 |
| [1, 1, 0] | 100% | 两个有用片段都排在前面，不代表第三个片段有用 |
| [0, 0] | 0% | 没有片段被判断为有用 |
| [] | 0% | 原版对空列表返回0，不能解释成模型检查过任何chunk |

这些计算例子使用手工给定的0/1序列，只验证公式，不是模型评分或人工业务审核。

因此，该指标不是简单的“有用chunk数 / 全部chunk数”，也不能单独证明没有噪声、证据完整或答案正确。需要结合关键证据召回率、Context Recall及回答指标阅读。

## 8. 适用性、缺失与汇总

- 本项目默认评价筛选后实际用于回答的context，保持原顺序，不重新检索、不补入源文档、不重新排序。
- 必须区分“已完整采集，确认为空列表”与“未采集到日志”。前者记录原版0分并标记筛选后正文为空；后者为不可测。
- 标准答案主要要求拒答或澄清的题，应按五指标方案单列适用性，不能机械当作普通事实问答混入总分。
- 模型调用失败、非法verdict或空理由单列错误，不记0分或满分。
- 有效单题分数做宏平均，同时报告有效题数、不适用数、不可测及失败数。
- 汇总中不得把实际只有2或3个chunk的题写成固定Top5；K使用该题真实数量。

## 9. 应保存哪些评分依据

每题保存题号、运行ID、标准答案版本、context阶段、chunk数量及最终分数。每个chunk保存实际排名、context编号、来源、正文hash、verdict、reason和对应模型调用记录。完整正文沿用已冻结的推理集。

这些字段由程序附加，不要求裁判重新生成题号、排名或来源。原始模型输出必须保留，不把本次模板整理写成已正式评分。

## 10. 文件与来源

- [完整可调用提示词](可调用模板/context_precision_with_reference.prompt.template.txt)
- [结构化原版提示词、Schema与全部示例](上游原文/ragas_0.3.1_context_precision_prompt.json)
- [原版源码快照](上游原文/_context_precision.py)
- [来源与校验记录](来源与校验_context_precision_20260919.json)
- [五指标计算方案](../../第二步_五指标计算方案_v3.md)
- 官方定义：https://docs.ragas.io/en/v0.3.1/concepts/metrics/available_metrics/context_precision/

来源为本项目已安装的ragas==0.3.1。已比对安装分发包RECORD记录中的SHA-256，确认本次导出的源码和提示词拼装实现没有偏离安装包记录；这不是模型效果或业务正确性验证。
