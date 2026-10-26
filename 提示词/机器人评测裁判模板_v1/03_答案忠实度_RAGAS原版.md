# 答案忠实度：RAGAS Faithfulness 0.3.1 原版组件

用途：知识库内题中的有效样本。源版本选自当前OpenViking仓库uv.lock锁定的ragas==0.3.1。本次仅下载并校验源码，未安装依赖、未验证裁判运行环境。

## 推荐调用方式

正式评分使用固定版本的 `ragas.metrics._faithfulness.Faithfulness`，由RAGAS生成完整提示词并校验输出。不要把下述核心instruction单独发给模型后，就声称完整复用了原版。

原版完整提示词还包含输出JSON Schema、few-shot示例和输入包装，构造逻辑在上游原文/pydantic_prompt.py的PydanticPrompt.to_string。完整原始示例已原样导出到上游原文/ragas_0.3.1_faithfulness_prompts.json；原始类定义在上游原文/_faithfulness.py。

传入字段：

```json
{
  "user_input": "原问题",
  "response": "实际最终回答",
  "retrieved_contexts": ["本轮实际上下文片段1", "本轮实际上下文片段2"]
}
```

不传标准答案或required_facts作为事实支持依据。

## 第一步：拆分实际回答中的声明

组件：StatementGeneratorPrompt。

原始instruction：

```text
Given a question and an answer, analyze the complexity of each sentence in the answer. Break down each sentence into one or more fully understandable statements. Ensure that no pronouns are used in any statement. Format the outputs in JSON.
```

中文释义（仅供审核，不作为替换后的原版提示词）：根据问题和实际回答，把回答句子拆成可独立理解的声明，消除代词指代，输出JSON。不能添加回答原本没有的事实。

输入模型：

```json
{"question": "原问题", "answer": "机器人实际回答"}
```

输出模型：

```json
{"statements": ["声明1", "声明2"]}
```

上游示例：

```json
[
  {
    "input": {
      "question": "Who was Albert Einstein and what is he best known for?",
      "answer": "He was a German-born theoretical physicist, widely acknowledged to be one of the greatest and most influential physicists of all time. He was best known for developing the theory of relativity, he also made important contributions to the development of the theory of quantum mechanics."
    },
    "output": {
      "statements": [
        "Albert Einstein was a German-born theoretical physicist.",
        "Albert Einstein is recognized as one of the greatest and most influential physicists of all time.",
        "Albert Einstein was best known for developing the theory of relativity.",
        "Albert Einstein also made important contributions to the development of the theory of quantum mechanics."
      ]
    }
  }
]
```

## 第二步：判断每个声明是否有context支持

组件：NLIStatementPrompt。

原始instruction：

```text
Your task is to judge the faithfulness of a series of statements based on a given context. For each statement you must return verdict as 1 if the statement can be directly inferred based on the context or 0 if the statement can not be directly inferred based on the context.
```

中文释义（仅供审核）：只依据给定context，判断每条声明能否直接推断成立；能支持记1，否则记0。

输入模型：

```json
{"context": "按原版实现用换行拼接retrieved_contexts", "statements": ["第一步生成的声明1", "声明2"]}
```

输出模型：

```json
{"statements": [{"statement": "原始声明逐字保留", "reason": "简短理由", "verdict": 1}]}
```

上游示例：

```json
[
  {
    "input": {
      "context": "John is a student at XYZ University. He is pursuing a degree in Computer Science. He is enrolled in several courses this semester, including Data Structures, Algorithms, and Database Management. John is a diligent student and spends a significant amount of time studying and completing assignments. He often stays late in the library to work on his projects.",
      "statements": [
        "John is majoring in Biology.",
        "John is taking a course on Artificial Intelligence.",
        "John is a dedicated student.",
        "John has a part-time job."
      ]
    },
    "output": {
      "statements": [
        {
          "statement": "John is majoring in Biology.",
          "reason": "John's major is explicitly mentioned as Computer Science. There is no information suggesting he is majoring in Biology.",
          "verdict": 0
        },
        {
          "statement": "John is taking a course on Artificial Intelligence.",
          "reason": "The context mentions the courses John is currently enrolled in, and Artificial Intelligence is not mentioned. Therefore, it cannot be deduced that John is taking a course on AI.",
          "verdict": 0
        },
        {
          "statement": "John is a dedicated student.",
          "reason": "The context states that he spends a significant amount of time studying and completing assignments. Additionally, it mentions that he often stays late in the library to work on his projects, which implies dedication.",
          "verdict": 1
        },
        {
          "statement": "John has a part-time job.",
          "reason": "There is no information given in the context about John having a part-time job.",
          "verdict": 0
        }
      ]
    }
  },
  {
    "input": {
      "context": "Photosynthesis is a process used by plants, algae, and certain bacteria to convert light energy into chemical energy.",
      "statements": [
        "Albert Einstein was a genius."
      ]
    },
    "output": {
      "statements": [
        {
          "statement": "Albert Einstein was a genius.",
          "reason": "The context and statement are unrelated",
          "verdict": 0
        }
      ]
    }
  }
]
```

## 汇总与执行边界

单题分数=verdict为1的声明数÷声明总数；总分取有效题平均。保留两阶段原始输入输出和每条声明判定，不只保存最终小数。

- context未采集或明确不完整：不调用裁判，标不可测。完整捕获的空context与“未捕获”不能混同。
- 无声明时，原版返回NaN；对外报告为“不适用/无有效分数”，不能改成100%。
- 项目规则先排除空回复、纯故障兜底等无有效业务回答，单独统计；这是样本纳入规则，不是重写原版prompt。
- 调用/解析失败与NaN需要保留原因，不能都归为答案不忠实或默认0。
- 校验第二阶段声明与第一阶段一一对应、数量一致、verdict严格为0/1；不通过则重试或标评分失败，不静默吞掉遗漏声明。
- 中文翻译、增加examples或额外system指令，都构成提示词变更，必须另设版本并校准。
- 机器人2使用proxy_response，机器人4使用model_input_context时必须分列口径，不能把缺失的企微历史用标准答案补齐。
