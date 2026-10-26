"""Small filesystem and serialization contracts shared by the workflow."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


class WorkflowError(Exception):
    def __init__(self, code, message, *, scope=None, recovery="修正问题后继续", outcome="needs_user"):
        super().__init__(message)
        self.code, self.scope, self.recovery, self.outcome = code, scope, recovery, outcome

    def record(self):
        return {"code": self.code, "message": str(self), "scope": self.scope,
                "recovery_action": self.recovery, "outcome": self.outcome, "occurred_at": now()}


def now():
    return datetime.now(timezone.utc).isoformat()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowError("INVALID_JSON", f"重复JSON键：{key}")
        result[key] = value
    return result


def parse_json(text):
    def nonfinite(value):
        raise WorkflowError("INVALID_JSON", f"不允许非有限数字：{value}")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            nonfinite(value)
        return number
    try:
        return json.loads(text, object_pairs_hook=_object, parse_constant=nonfinite, parse_float=finite_float)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise WorkflowError("INVALID_JSON", "JSON无法完整解析") from exc


def read_json(path):
    try:
        return parse_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise WorkflowError("READ_FAILED", "无法读取完整UTF-8记录", scope=str(path)) from exc


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WorkflowError("INVALID_JSON", "数据不能规范化为UTF-8 JSON") from exc


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise WorkflowError("INVALID_ID", "标识只能包含英文字母、数字、下划线和连接符")
    return value


def relative_name(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise WorkflowError("UNSAFE_PATH", "无效相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise WorkflowError("UNSAFE_PATH", "路径必须是无越界的相对路径")
    return value


def no_symlinks(path):
    path = Path(os.path.abspath(path))
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise WorkflowError("UNSAFE_PATH", "不允许符号链接路径", scope=str(path))
    return path


def within(root, name):
    relative_name(name)
    root = no_symlinks(root)
    target = no_symlinks(root / name)
    if not target.is_relative_to(root):
        raise WorkflowError("UNSAFE_PATH", "路径越过工作区")
    return target


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, data, *, replace=True):
    path = no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # link is exclusive: another process/file cannot be overwritten.
            os.link(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_text(path, text, **kwargs):
    atomic_bytes(path, text.encode("utf-8"), **kwargs)


def write_json(path, value, **kwargs):
    canonical(value)
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", **kwargs)


def stable_copy(source, target, expected=None, *, replace=False):
    source, target = no_symlinks(source), no_symlinks(target)
    if source == target or (source.exists() and target.exists() and source.samefile(target)):
        raise WorkflowError("SOURCE_OVERWRITE", "源文件和目标不能是同一文件", scope=str(source))
    if not source.is_file() or not stat.S_ISREG(source.stat().st_mode):
        raise WorkflowError("SOURCE_MISSING", "输入不是可读取的普通文件", scope=str(source))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    initial = source.stat()
    first = hashlib.sha256()
    fd, temporary = tempfile.mkstemp(prefix=".copy-", dir=target.parent)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as outgoing, source.open("rb") as incoming:
            while block := incoming.read(1024 * 1024):
                first.update(block)
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        value = first.hexdigest()
        final = source.stat()
        if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns) != (
                final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
            raise WorkflowError("CONTENT_CHANGED", "复制期间输入变化", scope=str(source))
        if value != digest(temp) or value != digest(source) or (expected and value != expected):
            raise WorkflowError("CONTENT_CHANGED", "输入与确认内容不一致", scope=str(source))
        if replace:
            os.replace(temp, target)
        else:
            os.link(temp, target)
        _fsync_dir(target.parent)
        return {"sha256": value, "size": final.st_size}
    finally:
        if temp.exists():
            temp.unlink()


def artifact(root, path):
    path = no_symlinks(path)
    return {"path": str(path.relative_to(Path(root))), "sha256": digest(path), "size": path.stat().st_size}


def verify(root, reference):
    try:
        path = within(root, reference["path"])
        if path.stat().st_size != reference["size"] or digest(path) != reference["sha256"]:
            raise WorkflowError("INTEGRITY_ERROR", "产物指纹不一致", scope=reference["path"])
        return path
    except (OSError, KeyError, TypeError) as exc:
        raise WorkflowError("INTEGRITY_ERROR", "产物缺失或引用无效") from exc


def inventory(inputs):
    if not isinstance(inputs, list) or not inputs:
        raise WorkflowError("INPUT_REQUIRED", "必须提供文件或目录列表")
    if any(not isinstance(item, (str, os.PathLike)) or not str(item) for item in inputs):
        raise WorkflowError("INPUT_REQUIRED", "文件列表必须包含有效路径")
    roots = [no_symlinks(item) for item in inputs]
    common = Path(os.path.commonpath([p.parent for p in roots])) if len(roots) > 1 else None
    result, names = [], set()
    for source in roots:
        if source.is_dir():
            files = sorted(source.rglob("*"))
            base = common or source
        else:
            files, base = [source], common or source.parent
        for path in files:
            no_symlinks(path)
            if path.is_dir() or path.name == ".DS_Store":
                continue
            name = relative_name(path.relative_to(base).as_posix())
            normalized = unicodedata.normalize("NFC", name).casefold()
            if normalized in names:
                raise WorkflowError("PATH_COLLISION", "文件归属或规范化名称冲突", scope=name)
            names.add(normalized)
            if not path.is_file():
                raise WorkflowError("SOURCE_MISSING", "文件不存在", scope=str(path))
            result.append({"input_path": str(path), "name": name, "sha256": digest(path),
                           "size": path.stat().st_size})
    if not result:
        raise WorkflowError("INPUT_REQUIRED", "目录内没有可处理原件")
    return result
