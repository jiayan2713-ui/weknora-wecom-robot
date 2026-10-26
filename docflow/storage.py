"""Single-writer local records; reading status never repairs or creates state."""
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from .common import WorkflowError, no_symlinks, read_json, safe_id, within, write_json, now


class Store:
    def __init__(self, root):
        self.root = no_symlinks(root)

    def path(self, name):
        return within(self.root, name)

    def read(self, name):
        value = read_json(self.path(name))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise WorkflowError("SCHEMA_UNSUPPORTED", "记录版本不受支持", scope=name)
        return value

    def write(self, name, value):
        write_json(self.path(name), {**value, "schema_version": 1})

    def batch(self, batch_id):
        return self.read(f"batches/{safe_id(batch_id)}/batch.json")

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path(".write.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkflowError("WORKSPACE_BUSY", "另一个写操作正在执行", recovery="查询状态或稍后继续") from exc
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {now()}\n".encode())
            os.fsync(fd)
            yield
        finally:
            os.close(fd)

    def locked(self):
        path = self.path(".write.lock")
        if not path.exists():
            return False
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
        finally:
            os.close(fd)
