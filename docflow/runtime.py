"""Process stop gate and durable per-source request budgets for the sync adapter."""
from contextlib import contextmanager
import re
import json
from pathlib import Path
import shutil
import threading
import time

from .common import WorkflowError, digest, read_json, write_json, atomic_bytes

_STOP = threading.Event()
_SESSION = None


def request_stop():
    _STOP.set()


def reset_stop():
    _STOP.clear()


def check_stop():
    if _STOP.is_set():
        raise WorkflowError('PARSER_STOPPED', '停止派发新解析请求；已完成结果与请求账本保留')
    if _SESSION is not None and _SESSION.elapsed_before+time.monotonic()-_SESSION.started>=_SESSION.max_seconds:
        raise WorkflowError('PARSE_BUDGET_EXCEEDED','文件累计处理时间预算耗尽')


def wait_retry(seconds):
    if _STOP.wait(seconds):
        check_stop()


class ParseSession:
    def __init__(self, state_dir, source_hash, settings=None):
        settings = settings or {}
        if not isinstance(settings,dict) or set(settings)-{'max_file_requests','max_file_units','max_file_seconds','max_recovery_rounds'}:
            raise WorkflowError('CONFIG_ERROR','解析预算包含未知字段或类型不正确')
        if not re.fullmatch(r'[a-f0-9]{64}',source_hash):
            raise WorkflowError('INTEGRITY_ERROR','来源预算指纹不合法')
        self.root = Path(state_dir).resolve() / 'parse-runtime' / source_hash
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ledger = self.root / 'budget.json'
        self.data = read_json(self.ledger) if self.ledger.exists() else {
            'schema_version': 1, 'source_hash': source_hash, 'attempts': [], 'planned_units': 0,
            'elapsed_seconds': 0, 'recoveries': {}}
        if self.data.get('source_hash') != source_hash:
            raise WorkflowError('INTEGRITY_ERROR', '预算账本来源指纹不一致')
        self.max_requests = self._limit(settings, 'max_file_requests', 512, 10000)
        self.max_units = self._limit(settings, 'max_file_units', 1600, 10000)
        self.max_seconds = self._limit(settings, 'max_file_seconds', 1800, 86400)
        self.max_rounds = self._limit(settings, 'max_recovery_rounds', 2, 2)
        self.started = time.monotonic()
        self.elapsed_before = self.data.get('elapsed_seconds', 0)
        self.lock = threading.RLock()
        self.hits = 0
        self.new_attempts = 0
        self.actions = []

    @staticmethod
    def _limit(settings, key, default, maximum):
        value = settings.get(key, default)
        if type(value) is not int or not 0 < value <= maximum:
            raise WorkflowError('CONFIG_ERROR', '解析预算参数不合法', scope=key)
        return value

    def persist(self):
        self.data['elapsed_seconds'] = self.elapsed_before + time.monotonic() - self.started
        write_json(self.ledger, self.data)

    def register_plan(self, units):
        with self.lock:
            if len(units) > self.max_units:
                raise WorkflowError('PARSE_BUDGET_EXCEEDED', '文件准备单元超出预算')
            self.data['planned_units'] = max(self.data['planned_units'], len(units))
            self.persist()

    def reserve_attempt(self, request_hash, stage, number, started_at):
        check_stop()
        with self.lock:
            if (len(self.data['attempts']) >= self.max_requests or
                    self.elapsed_before + time.monotonic() - self.started > self.max_seconds):
                raise WorkflowError('PARSE_BUDGET_EXCEEDED', '本来源版本的模型调用或处理时间预算耗尽')
            self.data['attempts'].append({'request_hash': request_hash, 'stage': stage,
                'attempt': number, 'started_at': started_at})
            self.new_attempts += 1
            self.persist()

    def recovery_action(self, unit_key, action):
        with self.lock:
            history = self.data['recoveries'].setdefault(unit_key, [])
            # Replaying a persisted action is free; only distinct strategy upgrades spend rounds.
            if action not in history:
                if len(history) >= self.max_rounds:
                    raise WorkflowError('PARSE_BUDGET_EXCEEDED', '该原始单元的恢复阶段预算耗尽')
                history.append(action)
                self.persist()
            self.actions.append({'unit_id': unit_key, 'action': action})

    def adopt_calls(self, directory):
        for file in Path(directory).rglob('call.json'):
            value = read_json(file)
            key = value.get('request_hash', '')
            if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                raise WorkflowError('INTEGRITY_ERROR', '旧调用记录身份不合法')
            with self.request_directory(key, file.parent, mirror=False):
                pass

    def saved_body_response(self, unit, image, config):
        import base64,hashlib
        uri='data:image/png;base64,'+base64.b64encode(Path(image).read_bytes()).decode()
        expected=[{'type':'text','text':unit.get('label','')+'\n'+unit.get('text','')},
                  {'type':'image_url','image_url':{'url':{'data_uri_sha256':hashlib.sha256(uri.encode()).hexdigest(),
                    'encoded_characters':len(uri),'source':'prepared image artifact'}}}]
        keys={a['request_hash'] for a in self.data['attempts'] if a.get('stage')=='body-format-recovery-v1'}
        for key in keys:
            directory=self.root/'requests'/key
            if not (directory/'request.json').is_file() or not (directory/'response.json').is_file():continue
            payload=read_json(directory/'request.json').get('payload',{})
            if any(payload.get(k)!=config.get(k) for k in ('model','temperature','max_tokens')):continue
            messages=payload.get('messages',[])
            if len(messages)!=2 or messages[1].get('content')!=expected:continue
            call=read_json(directory/'call.json');response=directory/'response.json'
            if digest(response)!=call.get('response_sha256'):
                raise WorkflowError('INTEGRITY_ERROR','匹配原始输入的旧补救响应指纹损坏')
            self.hits+=1
            return response
        return None

    def summary(self):
        return {'cache_hits': self.hits, 'model_attempts': self.new_attempts,
                'budget_used': len(self.data['attempts']), 'budget_limit': self.max_requests,
                'recovery_actions': self.actions, 'elapsed_seconds': round(time.monotonic() - self.started, 3)}

    @contextmanager
    def request_directory(self, request_hash, local, *, mirror=True):
        """Canonical request history survives retry job/path changes; publications stay separate."""
        local = Path(local).resolve()
        target = self.root / 'requests' / request_hash
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        # All calls for a payload share one writer; OS lock also protects future parallel readers.
        import fcntl
        with (target / 'writer.lock').open('a') as guard:
            while True:
                try:
                    fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    check_stop()
                    _STOP.wait(.1)
            if (local/'call.json').is_file():
                old = read_json(local/'call.json')
                if old.get('request_hash') != request_hash:
                    raise WorkflowError('MODEL_INPUT_CHANGED', '旧请求与本次实际输入不一致')
                response = local/'response.json'
                if old.get('response_sha256') and (not response.is_file() or digest(response) != old['response_sha256']):
                    raise WorkflowError('INTEGRITY_ERROR', '旧模型响应指纹不一致')
                with self.lock:
                    known={(a['request_hash'],a.get('attempt'),a.get('started_at')) for a in self.data['attempts']}
                    for attempt in old.get('attempts',[])+old.get('connection_failures',[]):
                        entry={'request_hash':request_hash,'stage':local.name,'attempt':attempt['number'],
                               'started_at':attempt.get('started_at'),'imported':True}
                        identity=(request_hash,entry['attempt'],entry['started_at'])
                        if identity not in known:
                            self.data['attempts'].append(entry);known.add(identity)
                    self.persist()
                if not (target/'call.json').exists():
                    for name in ('request.json','response.json','call.json'):
                        if not (local/name).is_file():continue
                        if (target/name).exists():
                            if digest(target/name)!=digest(local/name):
                                raise WorkflowError('INTEGRITY_ERROR','未完成的旧请求导入存在不同数据；保留证据')
                        else:atomic_bytes(target/name,(local/name).read_bytes(),replace=False)
            if mirror and (target/'response.json').is_file():
                self.hits += 1
            try:
                yield target
            finally:
                # Mirrors retain backwards-compatible job-local receipts without mutating old jobs.
                if mirror:
                    local.mkdir(parents=True, exist_ok=True)
                    for name in ('request.json', 'response.json', 'call.json'):
                        if (target/name).is_file():
                            atomic_bytes(local/name, (target/name).read_bytes())
                    write_json(local/'request-ref.json', {'request_hash': request_hash,
                        'source_hash': self.data['source_hash'], 'request_store': str(target)})


@contextmanager
def session(state_dir, source_hash, settings=None):
    global _SESSION
    if _SESSION is not None:
        raise WorkflowError('CONCURRENT_PARSE_SCOPE', '一个同步worker只能有一个文档解析控制上下文')
    current = ParseSession(state_dir, source_hash, settings)
    _SESSION = current
    try:
        yield current
    finally:
        try:
            with current.lock:current.persist()
        finally:
            _SESSION = None


def current_session():
    return _SESSION


@contextmanager
def request_directory(request_hash, local):
    if _SESSION is None:
        yield local
    else:
        with _SESSION.request_directory(request_hash, local) as directory:
            yield directory


def register_plan(units):
    if _SESSION is not None:
        _SESSION.register_plan(units)


def reserve_attempt(request_hash, stage, number, started_at):
    if _SESSION is not None:
        _SESSION.reserve_attempt(request_hash, stage, number, started_at)


def recovery_action(unit_key, action):
    if _SESSION is not None:
        _SESSION.recovery_action(unit_key, action)


def bounded_timeout(seconds):
    check_stop()
    if _SESSION is None:return seconds
    remaining=_SESSION.max_seconds-_SESSION.elapsed_before-(time.monotonic()-_SESSION.started)
    return min(seconds,max(.1,remaining))
