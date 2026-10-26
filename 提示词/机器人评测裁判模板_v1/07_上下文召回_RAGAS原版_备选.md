# 原版 RAGAS Context Recall：备选模板

版本：RAGAS 0.3.1，与本包Faithfulness保持一致。性质：单独增加的备选口径；不替换01中的固定required_facts覆盖率，不自动增加第七项主指标。

## 最重要的输入区别

这里检查的是审核后的标准答案reference，不是机器人实际回答response。源代码的内部字段虽然叫answer，但传入的值实际是row["reference"]。

```json
{
  "user_input": "原问题",
  "reference": "人工审核后的标准答案",
  "retrieved_contexts": ["本轮实际上下文片段1", "片段2"]
}
```

输入不需要required_facts，也不需要机器人实际回答。

## 原始核心指令

来源：ragas.metrics._context_recall.ContextRecallClassificationPrompt。

```text
Given a context, and an answer, analyze each sentence in the answer and classify if the sentence can be attributed to the given context or not. Use only 'Yes' (1) or 'No' (0) as a binary classification. Output json with reason.
```

中文释义（仅便于审阅，不替换原文）：依据给定context，逐句分析参考答案，判断每句是否能归因于该context；能支持记1，不能支持记0，并输出带理由的JSON。

精确口径说明：这个版本的原始指令是“analyze each sentence”，不是Faithfulness那样先调用单独的原子声明拆分步骤。此前用“评分器识别事实”作了概念简化；实际分母由该版本裁判输出的classifications决定，不保证与人工required_facts数量相同。

## 输入包装

原版执行时将数据映射为：

```json
{
  "question": "user_input",
  "context": "用换行拼接retrieved_contexts",
  "answer": "reference，不是机器人回答"
}
```

## 原始输出结构

```json
{
  "classifications": [
    {
      "statement": "参考答案中的待检查陈述",
      "reason": "简短判定理由",
      "attributed": 1
    }
  ]
}
```

attributed应为0或1。原始完整示例：

```json
[
  {
    "input": {
      "question": "What can you tell me about albert Albert Einstein?",
      "context": "Albert Einstein (14 March 1879 - 18 April 1955) was a German-born theoretical physicist, widely held to be one of the greatest and most influential scientists of all time. Best known for developing the theory of relativity, he also made important contributions to quantum mechanics, and was thus a central figure in the revolutionary reshaping of the scientific understanding of nature that modern physics accomplished in the first decades of the twentieth century. His mass-energy equivalence formula E = mc2, which arises from relativity theory, has been called 'the world's most famous equation'. He received the 1921 Nobel Prize in Physics 'for his services to theoretical physics, and especially for his discovery of the law of the photoelectric effect', a pivotal step in the development of quantum theory. His work is also known for its influence on the philosophy of science. In a 1999 poll of 130 leading physicists worldwide by the British journal Physics World, Einstein was ranked the greatest physicist of all time. His intellectual achievements and originality have made Einstein synonymous with genius.",
      "answer": "Albert Einstein born in 14 March 1879 was  German-born theoretical physicist, widely held to be one of the greatest and most influential scientists of all time. He received the 1921 Nobel Prize in Physics for his services to theoretical physics. He published 4 papers in 1905.  Einstein moved to Switzerland in 1895"
    },
    "output": {
      "classifications": [
        {
          "statement": "Albert Einstein, born on 14 March 1879, was a German-born theoretical physicist, widely held to be one of the greatest and most influential scientists of all time.",
          "reason": "The date of birth of Einstein is mentioned clearly in the context.",
          "attributed": 1
        },
        {
          "statement": "He received the 1921 Nobel Prize in Physics for his services to theoretical physics.",
          "reason": "The exact sentence is present in the given context.",
          "attributed": 1
        },
        {
          "statement": "He published 4 papers in 1905.",
          "reason": "There is no mention about papers he wrote in the given context.",
          "attributed": 0
        },
        {
          "statement": "Einstein moved to Switzerland in 1895.",
          "reason": "There is no supporting evidence for this in the given context.",
          "attributed": 0
        }
      ]
    }
  }
]
```

## 如何正式复用

使用固定版本 `ragas.metrics._context_recall.LLMContextRecall`，由RAGAS构造完整提示词。OpenViking当前适配代码导入的ContextRecall是继承该实现的兼容类。

本页核心instruction不是完整模型请求。原版请求还包含输出JSON Schema、上述示例和PydanticPrompt.to_string的输入包装。对应源码已保存在上游原文/_context_recall.py和pydantic_prompt.py。不要把中文释义或单独一段instruction手动调用后称为严格原版运行。

## 分数计算

单题=被支持的分类条目数/全部分类条目数。没有有效分类时返回NaN。多次生成时原实现先按attributed进行ensemble，再计算分数；应保留原运行配置和原始判定，不自行改分母。

本项目汇总使用有效题算术平均，并报告有效题数/40。缺失context、分类错误、无有效分类及待复核分别统计，不默认为0或100%。完整捕获且确认context为空时才可按项目规则明确记录未召回。

## 与当前主方案关系

- 01固定必要事实覆盖率：事实列表由人工提前审核并冻结。
- 本文件原版Context Recall：根据参考答案逐句判定，分类数量由评分器输出决定。
- 二者衡量目标相近，评分清单和分母可能不同。需要对照时分别命名、分别保存，不互相替换分数，也不在一个召回率中混算。

## 来源与状态

来源为OpenViking uv.lock所锁定、经SHA-256验证的PyPI ragas==0.3.1 wheel。只提取和整理提示词数据，未安装依赖、未调用裁判、未进行人工校准。
