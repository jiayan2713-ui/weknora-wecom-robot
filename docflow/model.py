"""Model calls with persistent attempt budgets and a separate durability boundary."""
from __future__ import annotations

import socket
import json
import hashlib
import math
import re
import os
import shutil
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

from .reliability import http_category
from .common import WorkflowError, atomic_bytes, canonical, digest, now, read_json, write_json, parse_json


class ModelClient:
    def __init__(self, config, transport=None, sleep=time.sleep, retry_terminal=False):
        self.config, self.transport, self.sleep = config, transport or self._http, sleep
        self.real_transport, self.retry_terminal = transport is None, retry_terminal
        self.volatile = {}

    def _http(self, encoded):
        key = os.environ.get(self.config["api_key_env"], "")
        if not key:
            raise WorkflowError("CREDENTIAL_MISSING", "尚未配置百炼凭据环境变量", scope=self.config["api_key_env"])
        request = urllib.request.Request(self.config["endpoint"], data=encoded,
                                         headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        # Disable redirects: credentials must never be forwarded to a different endpoint.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect())
        from .runtime import bounded_timeout
        with opener.open(request, timeout=bounded_timeout(self.config["timeout_seconds"])) as response:
            body = response.read(16 * 1024 * 1024 + 1)
            if len(body) > 16 * 1024 * 1024:
                raise WorkflowError("RESPONSE_TOO_LARGE", "模型响应超过本地接收限制")
            return body, response.status, response.headers.get("x-request-id") or response.headers.get("x-dashscope-request-id")

    def _extract(self, body):
        try:
            result = parse_json(body.decode("utf-8"))
        except UnicodeError as exc:
            raise WorkflowError("MODEL_RESPONSE_INVALID", "模型响应不是UTF-8") from exc
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise WorkflowError("MODEL_TRUNCATED", "模型输出未正常完成；保留响应，不自动重复调用")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("content")
            return content, result.get("usage")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise WorkflowError("MODEL_RESPONSE_INVALID", "模型响应结构无效；已保留原响应") from exc

    def call(self, directory, stage, messages):
        directory = Path(directory) / stage
        directory.mkdir(parents=True, exist_ok=True)
        key = os.environ.get(self.config["api_key_env"], "")
        if key:
            def redact(value):
                if isinstance(value, dict):
                    if value.get("type") == "image_url":
                        return value
                    return {k: redact(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [redact(v) for v in value]
                return value.replace(key, "[credential-redacted]") if isinstance(value, str) else value
            messages = redact(messages)
        payload = {"model": self.config["model"], "messages": messages, "temperature": self.config["temperature"],
                   "max_tokens": self.config["max_tokens"], "enable_thinking": False, "enable_search": False}
        encoded = canonical(payload)
        if len(encoded) > 16 * 1024 * 1024:
            raise WorkflowError("CAPACITY_EXCEEDED", "单元请求过大，需拆分")
        request_hash = hashlib.sha256(self.config["endpoint"].encode() + b"\0" + encoded).hexdigest()
        from .runtime import request_directory
        try:
            with request_directory(request_hash, directory) as actual:
                return self._call_persisted(actual, payload, encoded, request_hash, key, stage)
        except WorkflowError as error:
            if not isinstance(error.scope,dict):error.scope={}
            error.scope.update(stage=stage,unit_id=directory.parent.name,request_hash=request_hash)
            if error.code=='MODEL_OUTCOME_UNKNOWN':error.scope['outcome_certainty']='unknown'
            raise

    def _call_persisted(self, directory, payload, encoded, request_hash, key, stage):
        state_path, response_path = directory / "call.json", directory / "response.json"
        state = read_json(state_path) if state_path.exists() else {"request_hash": request_hash, "attempts": []}
        if not isinstance(state, dict) or not isinstance(state.get('request_hash'), str) or not re.fullmatch(r'[a-f0-9]{64}', state['request_hash']) or not isinstance(state.get('attempts'), list):
            raise WorkflowError('INTEGRITY_ERROR', '模型调用记录Schema损坏')
        for i, entry in enumerate(state['attempts'], 1):
            if not isinstance(entry, dict) or type(entry.get('number')) is not int or entry['number'] != i or entry.get('status') not in {'in_flight','unknown','received','failed'}:
                raise WorkflowError('INTEGRITY_ERROR', '模型尝试记录损坏')
        if len(state['attempts']) > 3 or ('response_sha256' in state and (not isinstance(state['response_sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',state['response_sha256']))):
            raise WorkflowError('INTEGRITY_ERROR', '模型响应身份或尝试数损坏')
        if 'retry_at' in state and (type(state['retry_at']) not in {int,float} or not math.isfinite(state['retry_at'])):
            raise WorkflowError('INTEGRITY_ERROR', '模型重试时间损坏')
        if 'terminal_error' in state and not isinstance(state['terminal_error'],str):
            raise WorkflowError('INTEGRITY_ERROR', '模型错误记录损坏')
        if state["request_hash"] != request_hash:
            raise WorkflowError("MODEL_INPUT_CHANGED", "已有单元请求输入变化，应创建新解析版本")
        if state.get("response_sha256"):
            if not response_path.is_file() or digest(response_path) != state["response_sha256"]:
                raise WorkflowError("INTEGRITY_ERROR", "已持久化模型响应损坏")
            return self._extract(response_path.read_bytes())[0]
        # A complete response may have landed just before a process died while publishing call.json.
        if response_path.exists():
            state.update(response_sha256=digest(response_path), outcome_certainty="response_durable")
            write_json(state_path, state)
            return self._extract(response_path.read_bytes())[0]
        changed = False
        for attempt in state["attempts"]:
            if attempt["status"] == "in_flight":
                attempt.update(status="unknown", outcome_certainty="unknown")
                changed = True
        if changed:
            write_json(state_path, state)
        if not isinstance(state.get('connection_failures',[]),list) or len(state.get('connection_failures',[]))>512:
            raise WorkflowError('INTEGRITY_ERROR','网络恢复历史损坏')
        unknown = any(a.get('status') == 'unknown' or a.get('outcome_certainty') == 'unknown'
                      for a in state['attempts'])
        if unknown and request_hash not in self.volatile and not self.retry_terminal:
            raise WorkflowError('MODEL_OUTCOME_UNKNOWN', '先前请求已发送但结果未知；普通恢复不会重发',
                                scope={'call_record': str(state_path), 'attempts': len(state['attempts'])})
        if state.get("terminal_error") and not self.retry_terminal:
            raise WorkflowError(state["terminal_error"], "上次请求已停止，普通恢复不会重复调用",
                                scope={"http_status": state["attempts"][-1].get("http_status"),
                                       "category": state["attempts"][-1].get("category")} if state["attempts"] else None)
        if self.retry_terminal:
            state.pop("terminal_error", None)
        if len(state["attempts"]) >= self.config["max_attempts"] and request_hash not in self.volatile:
            raise WorkflowError("ATTEMPTS_EXHAUSTED", "该单元已用尽实际尝试次数")
        if state.get("retry_at", 0) > time.time():
            raise WorkflowError("RETRY_AFTER", "供应商要求稍后重试；当前未再次调用")
        def audit(value):
            if isinstance(value, dict):
                return {k: audit(v) for k,v in value.items()}
            if isinstance(value, list):
                return [audit(v) for v in value]
            if isinstance(value, str) and value.startswith("data:image/"):
                return {"data_uri_sha256": hashlib.sha256(value.encode()).hexdigest(), "encoded_characters": len(value), "source": "prepared image artifact"}
            return value
        write_json(directory / "request.json", {"endpoint": self.config["endpoint"], "request_hash": request_hash, "payload": audit(payload)})
        raw = self.volatile.get(request_hash)
        while raw is None and len(state["attempts"]) < self.config["max_attempts"]:
            from .runtime import check_stop, reserve_attempt
            check_stop()
            if self.real_transport and not key:
                raise WorkflowError("CREDENTIAL_MISSING", "尚未配置百炼凭据环境变量", scope=self.config["api_key_env"])
            if shutil.disk_usage(directory).free < 64 * 1024 * 1024:
                raise WorkflowError('DISK_SPACE_LOW', '剩余空间不足64MiB，已停止后续模型调用')
            attempt = {"number": len(state["attempts"]) + 1, "started_at": now(), "status": "in_flight"}
            reserve_attempt(request_hash, stage, attempt['number'], attempt['started_at'])
            state["attempts"].append(attempt)
            write_json(state_path, state)
            started = time.monotonic()
            try:
                raw, status, provider_id = self.transport(encoded if self.real_transport else payload)
            except WorkflowError as exc:
                attempt.update(status="failed", seconds=time.monotonic()-started, code=exc.code)
                state["terminal_error"] = exc.code
                write_json(state_path, state)
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                status = getattr(exc, "code", None)
                error_body = ""
                provider_id = None
                if isinstance(exc, urllib.error.HTTPError):
                    try:
                        error_body = exc.read(65536).decode("utf-8", "replace")
                    except OSError:
                        error_body = "[error response unavailable]"
                    if key:
                        error_body = error_body.replace(key, "[credential-redacted]")
                    provider_id = exc.headers.get("x-request-id") if exc.headers else None
                    write_json(directory / ("error-%03d.json" % attempt["number"]),
                               {"http_status": status, "body": error_body,
                                "provider_request_id": provider_id, "category": http_category(status, error_body)})
                reason=getattr(exc,'reason',None)
                not_sent=isinstance(reason,(socket.gaierror,ConnectionRefusedError)) or isinstance(exc,ConnectionRefusedError)
                category = http_category(status, error_body)
                transient = status is None or status in {408, 429} or (isinstance(status, int) and status >= 500)
                attempt.update(status="failed", seconds=time.monotonic()-started, http_status=status,
                               outcome_certainty="not_sent" if not_sent else "unknown" if status is None else "http_error",
                               category=category, provider_request_id=provider_id)
                delay = min(2 ** attempt["number"], 30)
                retry_after = getattr(exc, "headers", {}).get("Retry-After") if getattr(exc, "headers", None) else None
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                        except (ValueError, TypeError):
                            pass
                if not transient:
                    state["terminal_error"] = "MODEL_HTTP_ERROR"
                state["retry_at"] = time.time() + max(0, delay)
                write_json(state_path, state)
                if not_sent:
                    # No HTTP request was sent: preserve the network history, not a paid-request attempt.
                    state['attempts'].pop()
                    failures=state.setdefault('connection_failures',[])
                    failures.append({**attempt,'connection_number':len(failures)+1})
                    state['outcome_certainty']='not_sent'
                    state['retry_at']=time.time()+min(1800,30*2**min(len(failures)-1,6))
                    write_json(state_path,state)
                    if len(failures)>=512:
                        raise WorkflowError('PARSE_BUDGET_EXCEEDED','网络连接恢复预算耗尽，历史保留') from None
                    raise WorkflowError('RETRY_AFTER','连接尚未建立，保留任务等待网络恢复',scope={'call_record':str(state_path),'outcome_certainty':'not_sent'}) from None
                if status is None and not self.retry_terminal:
                    raise WorkflowError('MODEL_OUTCOME_UNKNOWN', '请求结果未知，保留账本，不自动重发',
                                        scope={'call_record': str(state_path)}) from None
                if not transient or len(state["attempts"]) >= self.config["max_attempts"]:
                    raise WorkflowError("MODEL_HTTP_ERROR", f"模型请求失败（HTTP {status or 'unknown'}），已记录实际尝试次数",
                                        scope={"http_status": status, "category": category, "call_record": str(state_path)}) from None
                if delay > 60:
                    raise WorkflowError("RETRY_AFTER", "供应商要求等待超过60秒，已暂停自动重试") from None
                if self.sleep is time.sleep:
                    from .runtime import wait_retry
                    wait_retry(delay)
                else:
                    self.sleep(delay)
                continue
            attempt.update(status="received", seconds=time.monotonic()-started, http_status=status, provider_request_id=provider_id)
            if key:
                raw = raw.replace(key.encode(), b"[credential-redacted]")
            self.volatile[request_hash] = raw
        if raw is None:
            write_json(state_path, state)
            raise WorkflowError("ATTEMPTS_EXHAUSTED", "该单元已用尽实际尝试次数；没有可靠响应可复用")
        # No network retry surrounds this block. Persisting a received response can fail without paying again.
        state["outcome_certainty"] = "response_received_volatile"
        try:
            atomic_bytes(response_path, raw, replace=False)
            state.update(response_sha256=digest(response_path), outcome_certainty="response_durable")
            try:
                _, state["usage"] = self._extract(raw)
            except WorkflowError:
                state["usage"] = None
            write_json(state_path, state)
        except OSError as exc:
            raise WorkflowError("RESPONSE_STORAGE_FAILED", "已收到模型响应但保存失败；当前进程不会再次调用模型", outcome="failed") from exc
        self.volatile.pop(request_hash, None)
        return self._extract(raw)[0]
