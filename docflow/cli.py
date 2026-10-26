import argparse
import json
import sys

from .common import WorkflowError, read_json
from .runner import Workflow


def main():
    parser = argparse.ArgumentParser(description="本地资料解析与评测表格工作流")
    parser.add_argument("operation", choices=["start", "status", "doctor", "approve", "submit", "resume", "import-review"])
    parser.add_argument("--root", help="批次数据根目录，默认项目workflow-data")
    parser.add_argument("--input", help="修改操作的JSON请求文件")
    parser.add_argument("--batch", help="status查询批次")
    args = parser.parse_args()
    try:
        request = read_json(args.input) if args.input else {"batch_id": args.batch}
        if args.operation not in {"status", "doctor"} and not args.input:
            raise WorkflowError("INPUT_REQUIRED", "修改操作需要--input请求文件")
        result = Workflow(args.root).execute(args.operation, request)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["outcome"] == "failed" or result.get('errors') else 0
    except WorkflowError as error:
        print(json.dumps({"interface_version": 1, "outcome": error.outcome, "error": error.record()}, ensure_ascii=False))
        return 2
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"interface_version": 1, "outcome": "failed", "error": {"code": "INVALID_STATE_OR_INPUT", "message": "输入、依赖或本地记录无效", "type": type(error).__name__}}, ensure_ascii=False))
        return 2
