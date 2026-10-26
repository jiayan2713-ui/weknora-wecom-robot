import multiprocessing
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from docflow.common import WorkflowError, atomic_text, digest, fingerprint, inventory, parse_json, stable_copy
from docflow.config import configuration, source_url
from docflow.storage import Store


def try_lock(root, queue):
    try:
        with Store(root).lock():
            queue.put("acquired")
    except WorkflowError as error:
        queue.put(error.code)


class StorageTests(unittest.TestCase):
    def test_canonical_and_config(self):
        self.assertEqual(fingerprint({"b": 1, "a": [2, 1]}), fingerprint({"a": [2, 1], "b": 1}))
        self.assertNotEqual(fingerprint([1, 2]), fingerprint([2, 1]))
        for raw in ['{"a":1,"a":2}', 'NaN', '1e1000']:
            with self.assertRaises(WorkflowError):
                parse_json(raw)
        self.assertEqual(configuration()["total_questions"], 50)
        with self.assertRaises(WorkflowError):
            configuration({"api_key": "never-store-keys"})
        for endpoint in ['https://:443', 'https://:secret@example.com/a']:
            with self.assertRaises(WorkflowError):
                configuration({"endpoint": endpoint})
        with self.assertRaises(WorkflowError):
            source_url('https://:secret@example.com', configuration())
        for bad in [[None], [1]]:
            with self.assertRaises(WorkflowError):
                inventory(bad)

    def test_stable_copy_and_source_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, target = root / "中文#%.txt", root / "copy.txt"
            source.write_text("原件")
            expected = digest(source)
            self.assertEqual(stable_copy(source, target, expected)["sha256"], expected)
            with self.assertRaises(FileExistsError):
                stable_copy(source, target)
            source.write_text("已修改")
            with self.assertRaises(WorkflowError):
                stable_copy(source, root / "other", expected)
            link = root / "link"
            link.symlink_to(source)
            with self.assertRaises(WorkflowError):
                inventory([str(link)])
            self.assertEqual(target.read_text(), "原件")
            before = source.stat()
            with self.assertRaises(WorkflowError):
                stable_copy(source, source, replace=True)
            self.assertEqual(source.stat().st_ino, before.st_ino)

    def test_multiple_roots_and_json_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            for name in ['甲', '乙']:
                (root/name).mkdir()
                (root/name/'same.txt').write_text(name)
            self.assertEqual([x['name'] for x in inventory([root/'甲', root/'乙'])], ['甲/same.txt', '乙/same.txt'])
            store = Store(root/'state')
            store.write('batches/test/batch.json', {'id': 'test'})
            self.assertEqual(store.batch('test')['id'], 'test')
            target=store.path('batches/test/batch.json')
            old=target.read_bytes()
            with patch('docflow.common.os.replace', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    store.write('batches/test/batch.json', {'id': 'changed'})
            self.assertEqual(target.read_bytes(), old)
            target.write_text('{"schema_version":2}')
            with self.assertRaises(WorkflowError):
                store.batch('test')
            target.write_bytes(b'\xff')
            with self.assertRaises(WorkflowError):
                store.batch('test')

    def test_lock_is_cross_process_and_reads_do_not_create(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "state"
            store = Store(root)
            self.assertFalse(store.locked())
            self.assertFalse(root.exists())
            with store.lock():
                queue = multiprocessing.get_context("spawn").Queue()
                process = multiprocessing.get_context("spawn").Process(target=try_lock, args=(str(root), queue))
                process.start()
                self.assertEqual(queue.get(timeout=10), "WORKSPACE_BUSY")
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertFalse(store.locked())


if __name__ == "__main__":
    unittest.main()
