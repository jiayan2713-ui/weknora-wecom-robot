"""Mechanical checks only: semantic support and necessity still need human review."""
import json
import re
from collections import Counter

FIELDS = ["id", "question", "answer", "evidence", "required_facts", "category", "推理步数"]
LEGACY_XLSX_FIELDS = ["id", "question", "answer", "evidence", "original_evidence", "required_facts", "category", "推理步数"]
XLSX_FIELDS = LEGACY_XLSX_FIELDS[:6] + ["审核正文摘录"] + LEGACY_XLSX_FIELDS[6:]
SUPPORTED_CANDIDATE_SCHEMAS = {"candidate_v2", "candidate_v3"}


def excel_error(text):
    if not isinstance(text, str):
        return "字段必须是字符串"
    if any(ord(c) < 32 and c not in "\t\n\r" or 0xD800 <= ord(c) <= 0xDFFF or ord(c) in {0xFFFE, 0xFFFF} for c in text):
        return "包含Excel不支持的字符"
    if len(text.encode("utf-16-le")) // 2 > 32767 or text.count("\n") > 253:
        return "超过Excel单元格容量"
    return None


def original_text(item, table):
    if not item["evidence"]:
        return "不适用（知识库外，无直接证据）"
    return "\n\n".join(f'{table[key]["original_name"]}｜{table[key]["location"]}\n{table[key]["url"] or "未提供来源URL"}' for key in item["evidence"])


def fact_quotes(facts):
    """Group identical source excerpts without losing their fact associations."""
    groups = {}
    for record in sorted(facts, key=lambda value: (value["question_id"], value["fact_index"])):
        entries = groups.setdefault(record["question_id"], {})
        for source in record["sources"]:
            indices = entries.setdefault((source["evidence"], source["quote"]), [])
            if record["fact_index"] not in indices:
                indices.append(record["fact_index"])
    return {identifier: "\n\n".join(
        f'【事实{"、".join(map(str, indices))}｜{key}】\n{quote}'
        for (key, quote), indices in entries.items()) for identifier, entries in groups.items()}


def row_values(item, table, quotes=None):
    values = dict(item)
    values["evidence"] = json.dumps(item["evidence"], ensure_ascii=False)
    values["required_facts"] = "\n".join(f"{i+1}. {fact}" for i, fact in enumerate(item["required_facts"])) or "[]"
    values["original_evidence"] = original_text(item, table)
    if quotes is None:
        return [values[name] for name in LEGACY_XLSX_FIELDS]
    values["审核正文摘录"] = quotes.get(item["id"], "不适用（知识库外）" if item["category"] == "知识库外" else "未提供（旧版候选无事实摘录）")
    return [values[name] for name in XLSX_FIELDS]


def validate(candidate, facts, generation, table, *, include_quotes=True):
    errors = []
    def error(code, message, question=None, field=None, fact_index=None):
        errors.append({"code": code, "message": message, "question_id": question, "field": field, "fact_index": fact_index})
    if not isinstance(candidate, list):
        error("CANDIDATE_SCHEMA", "候选必须是完整JSON数组")
        return errors
    if not isinstance(facts, list):
        error("FACT_SCHEMA", "fact_checks必须是完整JSON数组")
        return errors
    schema = generation.get("candidate_schema")
    if not isinstance(schema, str) or schema not in SUPPORTED_CANDIDATE_SCHEMAS:
        error("CANDIDATE_VERSION", "不支持该生成任务的候选规则版本")
        return errors
    # Frozen generations retain their original rules when resumed or reexported.
    fact_categories = {"知识库内", "模糊地带"} if schema == "candidate_v3" else {"知识库内"}
    config = generation["config"]
    if len(candidate) != config["total_questions"]:
        error("QUESTION_COUNT", "题数与本批配置不一致")
    width = max(3, len(str(config["total_questions"])))
    by_id, valid, categories, steps, questions = {}, [], Counter(), Counter(), Counter()
    for index, item in enumerate(candidate):
        expected_id = f'{config["id_prefix"]}-{index+1:0{width}}'
        if not isinstance(item, dict) or set(item) != set(FIELDS):
            error("CANDIDATE_SCHEMA", "每题必须恰为7字段；不接受旧交付快照", expected_id)
            continue
        identifier = item["id"]
        if identifier != expected_id:
            error("QUESTION_ID", "题号必须按配置连续排列", expected_id, "id")
        if not isinstance(identifier, str):
            continue
        if identifier in by_id:
            error("DUPLICATE_ID", "题号重复", identifier)
            continue
        by_id[identifier] = item
        strings_valid = True
        for field in ["id", "question", "answer", "category", "推理步数"]:
            value = item[field]
            if not isinstance(value, str) or not value.strip():
                error("FIELD_TYPE", "字段必须为非空字符串", identifier, field)
                strings_valid = False
            elif excel_error(value):
                error("EXCEL_CAPACITY", excel_error(value), identifier, field)
        lists_valid = True
        for field in ["evidence", "required_facts"]:
            value = item[field]
            if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
                error("FIELD_TYPE", "字段必须为字符串数组", identifier, field)
                lists_valid = False
            elif len(set(value)) != len(value):
                error("DUPLICATE_VALUE", "数组不能重复列入相同内容", identifier, field)
        if not strings_valid or not lists_valid:
            continue
        valid.append(item)
        categories[item["category"]] += 1
        steps[item["推理步数"]] += 1
        questions[item["question"].strip()] += 1
        if item["category"] == "知识库内":
            if item["推理步数"] not in {"单步", "多步"} or not item["required_facts"] or not item["evidence"]:
                error("IN_KB_REQUIREMENTS", "库内题需有效推理类型、非空事实及证据", identifier)
        elif item["category"] == "知识库外":
            if item["required_facts"] or item["推理步数"] != "不适用":
                error("BOUNDARY_REQUIREMENTS", "库外题facts为空且推理步数不适用", identifier)
            if item["evidence"]:
                error("OUTSIDE_EVIDENCE", "知识库外证据必须为空", identifier, "evidence")
        elif item["category"] == "模糊地带":
            if item["推理步数"] != "不适用":
                error("BOUNDARY_REQUIREMENTS", "模糊题推理步数为不适用", identifier)
            if not item["evidence"]:
                error("AMBIGUOUS_EVIDENCE", "模糊题仍需相关证据", identifier, "evidence")
            if bool(item["required_facts"]) != (schema == "candidate_v3"):
                message = "模糊题必须包含有原文依据的必要事实" if schema == "candidate_v3" else "旧版candidate_v2模糊题facts为空"
                error("AMBIGUOUS_FACTS", message, identifier, "required_facts")
        else:
            error("CATEGORY", "未知题目分类", identifier, "category")
        for key in item["evidence"]:
            if key not in table:
                error("EVIDENCE_UNKNOWN", "引用不在本批合法证据表", identifier, "evidence")
        if all(key in table for key in item["evidence"]):
            for field, value in zip(LEGACY_XLSX_FIELDS, row_values(item, table)):
                if excel_error(value):
                    error("EXCEL_CAPACITY", excel_error(value), identifier, field)
    for item in valid:
        if questions[item["question"].strip()] > 1:
            error("DUPLICATE_QUESTION", "问题文本完全重复", item["id"], "question")
    expected_categories = {"知识库内": config["in_kb_questions"], "知识库外": config["out_of_kb_questions"], "模糊地带": config["ambiguous_questions"]}
    if any(categories[key] != value for key, value in expected_categories.items()):
        error("CATEGORY_COUNTS", "三类题数不符合配置")
    if steps["单步"] != config["single_step_questions"] or steps["多步"] != config["multi_step_questions"]:
        error("REASONING_COUNTS", "单步/多步题数不符合配置")
    seen, cited, quote_records = set(), {}, []
    valid_ids = {x["id"] for x in valid}
    for record in facts:
        if not isinstance(record, dict) or set(record) != {"question_id", "fact_index", "fact", "sources"}:
            error("FACT_SCHEMA", "事实核对记录字段不符")
            continue
        identifier, position = record["question_id"], record["fact_index"]
        if not isinstance(identifier, str) or identifier not in valid_ids or type(position) is not int:
            error("FACT_REFERENCE", "事实题号或序号无效")
            continue
        item = by_id[identifier]
        pair = (identifier, position)
        if pair in seen:
            error("FACT_DUPLICATE", "事实序号重复", identifier, fact_index=position)
        seen.add(pair)
        if not 1 <= position <= len(item["required_facts"]) or item["category"] not in fact_categories:
            error("FACT_REFERENCE", "事实超出本题范围", identifier, fact_index=position)
            continue
        if record["fact"] != item["required_facts"][position-1]:
            error("FACT_MISMATCH", "事实文本与主数组不同", identifier, fact_index=position)
        sources = record["sources"]
        if not isinstance(sources, list) or not sources:
            error("FACT_SOURCES", "事实必须包含来源", identifier, fact_index=position)
            continue
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"evidence", "quote"} or not all(isinstance(source[x], str) and source[x].strip() for x in ["evidence", "quote"]):
                error("FACT_SOURCES", "事实来源格式无效", identifier, fact_index=position)
                continue
            key, quote = source["evidence"], source["quote"]
            quote_records.append({"question_id":identifier,"fact_index":position,"sources":[source]})
            if key not in item["evidence"] or key not in table:
                error("FACT_EVIDENCE", "事实引用必须属于本题及本批证据", identifier, fact_index=position)
            else:
                cited.setdefault(identifier, set()).add(key)
                if quote not in table[key]["body"]:
                    error("QUOTE_NOT_FOUND", "摘录不在该审核单元正文中", identifier, fact_index=position)
            for value in [record["fact"], key, quote]:
                if excel_error(value):
                    error("EXCEL_CAPACITY", excel_error(value), identifier, fact_index=position)
    for item in valid:
        for position in range(1, len(item["required_facts"]) + 1):
            if (item["id"], position) not in seen:
                error("FACT_MISSING", "缺少逐事实核对记录", item["id"], fact_index=position)
        if item["category"] in fact_categories and set(item["evidence"]) != cited.get(item["id"], set()):
            error("FACT_COVERAGE", "题目引用集合与事实来源不一致", item["id"], "evidence")
    covered = {table[key]["document_id"] for item in valid for key in item["evidence"] if key in table}
    for identifier in set(generation["required_documents"]) - covered:
        error("DOCUMENT_UNCOVERED", "必读文档没有实质引用候选，需核对覆盖", identifier)
    if include_quotes:
        for identifier, text in fact_quotes(quote_records).items():
            if excel_error(text):
                error("EXCEL_CAPACITY", excel_error(text), identifier, "审核正文摘录")
    return errors
