"""Bounded document pipeline with shared request/render budgets and one state writer."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from collections import deque
import copy
import queue
import threading

from .common import WorkflowError
from .model import ModelClient

from .reliability import FATAL_CODES, is_fatal


def ordered_bounded_map(function, items, workers):
    """At most workers submissions; yield in input order, cancel queued work on failure."""
    if workers == 1:
        for item in items:
            yield function(item)
        return
    iterator = iter(items)
    active = deque()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='docflow-render') as pool:
        try:
            for _ in range(workers):
                item = next(iterator, None)
                if item is not None:
                    active.append(pool.submit(function, item))
            while active:
                yield active.popleft().result()
                item = next(iterator, None)
                if item is not None:
                    active.append(pool.submit(function, item))
        finally:
            for future in active:
                future.cancel()


class LimitedClient:
    """Gate before ModelClient records an attempt, so cancellation spends no budget."""
    def __init__(self, client, semaphore, stopped):
        self.client, self.config = client, client.config
        self.semaphore, self.stopped = semaphore, stopped

    def call(self, *args, **kwargs):
        with self.semaphore:
            if self.stopped.is_set():
                raise WorkflowError('BATCH_STOPPED', '其他文档发生致命错误，尚未发送本请求')
            try:
                return self.client.call(*args, **kwargs)
            except WorkflowError as error:
                if is_fatal(error):
                    self.stopped.set()
                raise


def parse_parallel(workflow, batch, request, prompt):
    config = batch['config']
    request_slots = threading.BoundedSemaphore(config['parser_workers'])
    render_slots = threading.BoundedSemaphore(config.get('render_workers', 1))
    stopped = threading.Event()
    updates = queue.SimpleQueue()
    # The same source may occur at multiple paths; serialize its cache publication/reuse.
    source_locks = {}
    for doc in batch['documents']:
        source_locks.setdefault((doc['source']['sha256'], doc['extension']), threading.Lock())

    def process(index):
        local_doc = copy.deepcopy(batch['documents'][index])
        def checkpoint():
            updates.put((index, copy.deepcopy(local_doc)))
        client = (workflow.client_factory(config) if workflow.client_factory else
                  ModelClient(config, retry_terminal=request['args'].get('retry_failed', False)))
        client = LimitedClient(client, request_slots, stopped)
        error = None
        try:
            with source_locks[(local_doc['source']['sha256'], local_doc['extension'])]:
                if stopped.is_set():
                    return local_doc, None
                workflow._parse_one(batch, local_doc, prompt, client, checkpoint, render_slots)
        except Exception as caught:
            stopped.set()
            error = caught
        return local_doc, error

    completed_indices = set()
    def drain_updates():
        changed = False
        while True:
            try:
                index, snapshot = updates.get_nowait()
            except queue.Empty:
                break
            if index not in completed_indices:
                batch['documents'][index] = snapshot
                changed = True
        if changed:
            workflow._save_batch(batch)

    workers = config['document_workers']
    remaining = iter(range(len(batch['documents'])))
    first_error = None
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='docflow-document') as pool:
        active = {}
        try:
            for _ in range(workers):
                index = next(remaining, None)
                if index is not None:
                    active[pool.submit(process, index)] = index
            while active:
                finished, _ = wait(active, timeout=0.05, return_when=FIRST_COMPLETED)
                drain_updates()
                for future in finished:
                    index = active.pop(future)
                    local_doc, error = future.result()
                    batch['documents'][index] = local_doc
                    completed_indices.add(index)
                    workflow._save_batch(batch)
                    if error is not None and (first_error is None or
                            getattr(first_error, 'code', None) == 'BATCH_STOPPED'):
                        first_error = error
                if not stopped.is_set():
                    for _ in range(workers - len(active)):
                        index = next(remaining, None)
                        if index is None:
                            break
                        active[pool.submit(process, index)] = index
            drain_updates()
        finally:
            # A failed coordinator checkpoint must also stop workers before joining them.
            stopped.set()
            for future in active:
                future.cancel()
    if first_error is not None:
        raise first_error
