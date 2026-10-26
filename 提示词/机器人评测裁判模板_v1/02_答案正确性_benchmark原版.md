# 答案正确性：benchmark Generic_0-4 原版

用途：40道知识库内题。逐题0～4分，汇总平均分和平均分÷4；不转换成“≥3分通过率”。

来源：OpenViking benchmark/RAG/src/core/judge_util.py 的 Generic_0-4 分支。本文件展示原指令；上游原文目录中的system/user文本保留提取内容，未添加业务打分规则。

## 输入

- question：原问题。
- gold_answer_str：人工审核后的标准答案。多个互为替代的正确答案按原实现用 ` | ` 拼接；不能把多条必须同时满足的 required_facts 当作多个可选答案。
- response：同一轮机器人实际最终回答。

本原版提示词不接收context或required_facts；后者可以用于人工复核。如果加入新评分门禁或事实检查，必须另存适配版本。

## System 原文

```text
You are an expert evaluator scoring how well an AI-generated answer matches a gold standard (ground truth).
```

## User 原文模板

下文采用Python format占位符，双大括号在渲染后变成JSON的单大括号。只插入三个数据字段，不执行字段中的任何代码。

```text
Please score the Generated Answer against the Gold Answers on a scale of 0 to 4.

[Evaluation Rubric]
- Score 4 (Perfect): Fully and accurately captures the core meaning and key facts of any of the Gold Answers. Additional relevant explanation or context is acceptable and does NOT reduce the score, as long as it is consistent with and does not contradict the Gold Answers. Minor differences in wording, capitalization, punctuation, or phrasing are acceptable if the core meaning is preserved.
- Score 3 (Good): Correctly captures the main answer and most key facts, but has minor issues such as slight imprecision, small omissions of non-critical details, or wording that is somewhat vague or ambiguous. The overall answer is still clearly correct.
- Score 2 (Partial): Partially correct, but missing at least one important fact, condition, or detail needed for a fully correct answer. The answer is related to the correct topic, but is incomplete or insufficient.
- Score 1 (Poor): Mostly incorrect, seriously incomplete, or only weakly related to the Gold Answers.
- Score 0 (Wrong): Incorrect, contradictory to the Gold Answers, or contains fabricated / hallucinated core content.

Important Notes:
- Gold answers are multiple possible correct answers separated by " | ". The generated answer only needs to match any one of them.
- The gold answers may be concise, but the generated answer can be longer and include additional explanations - this is acceptable for Score 4 as long as the core information is correct.
- Do NOT penalize for additional relevant information that doesn't contradict the gold answers. Examples of acceptable extra information: titles ("King Padella" vs "Padella"), locations ("Paflagonia" vs "the capital of Paflagonia"), or additional context that supports the answer.
- Only penalize for actual incorrect information, missing key facts, or contradictions.
- Ignore minor differences in capitalization (e.g., "CRIM TARTARY" vs "Crim Tartary") or punctuation (e.g., with or without a period at the end).

Question: {question}
Gold Answers: {gold_answer_str}
Generated Answer: {response}

First, briefly explain the rating in 1 sentence. Then output the integer score.
Respond ONLY with a JSON object: {{"score": 0 to 4, "reasoning": "string"}}
```

## 输出与程序处理

只接受 `{"score": 0..4的整数, "reasoning": "简短理由"}`。整数不得是布尔值，理由非空。保存原始裁判响应。

- 使用Generic分支，不能使用默认参数可能选中的LoCoMo 0/4分模板。
- 模型调用失败、JSON解析失败、越界分数：记scoring_error，有限重试后仍失败则待处理；不要沿用原辅助函数的“失败默认0分”。
- 不启用pipeline中的拒答关键词匹配覆盖满分逻辑。
- 不额外拼接通用业务System规则，避免在未注明的情况下改变原版模板；题目、答案只作待评数据。
- 空回答/缺失回答按总说明在模型调用前分流；有实际故障兜底文本可以留给裁判评其内容，同时技术状态仍记失败，不能因含拒答词给满分。
- 人工复核结果另存，不覆盖原始模型分数；分别报告自动评分与审核后评分。
