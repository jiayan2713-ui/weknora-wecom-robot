"""Recoverable local-source sync with configurable, delayed deletion propagation."""
from contextlib import contextmanager
import sys
import argparse
import collections
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import time
import urllib.request
import unicodedata

from reconcile import API, hashes, save
from fill_missing import multipart
from original_ingest import wants_original
from transform_local import render
from parse_policy import ParseFailure, policy_signature, VERSION as PARSER_POLICY_VERSION
from replica_protocol import replica_state, deletion_receipt, ProtocolError
from failure_alerts import FailureAlerts, error_code
from operational_logs import OperationalLogs

SUPPORTED = {'.pdf', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls', '.csv', '.tsv',
             '.txt', '.md', '.markdown', '.png', '.jpg', '.jpeg', '.webp'}


def no_text_state(cfg):
    return 'no_text_skipped' if cfg.get('skip_no_text', True) else 'no_text_hold'


def normalized(value):
    return unicodedata.normalize('NFC', str(value))


def signature(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def references(records, identity):
    return sum(identity in r.get('direct_superseded_ids', []) or identity in r.get('remote_ids', []) or identity in r.get('pending', {}).get('remote_ids', []) or
               identity in r.get('pending', {}).get('superseded_ids', [])
               for r in records.values() if r.get('state') != 'deleted')


def rename_pairs(missing, fresh):
    """Only unambiguous content identity is admitted as a rename."""
    old = collections.defaultdict(list)
    new = collections.defaultdict(list)
    for identity, row in missing.items():
        old[row['source_hash']].append(identity)
    for relative, row in fresh.items():
        new[row['sha256']].append(relative)
    return [(old[h][0], new[h][0]) for h in old.keys() & new.keys()
            if len(old[h]) == len(new[h]) == 1]


@contextmanager
def writer_session(cfg):
    home = Path(cfg['state_dir']).resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home/'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Sync:
    def __init__(self, cfg, api=None, renderer=render, clock=time.time):
        self.cfg, self.clock, self.renderer = cfg, clock, renderer
        if cfg.get('source_mode', 'local') not in ('local', 'replica'):
            raise ValueError('unknown_source_mode')
        self.replica = None
        self.root = Path(cfg['source_root']).resolve()
        self.markdown = Path(cfg['markdown_root']).resolve()
        self.home = Path(cfg['state_dir']).resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = self.home / 'state.json'
        self.kb = cfg['knowledge_base_id']
        self.folder = cfg['folder_root'].strip('/')
        self.api = api or API(cfg['api_key_file'], cfg['weknora_base_url'], kb_id=cfg['knowledge_base_id'])
        self.state = json.loads(self.file.read_text()) if self.file.exists() else {
            'identity': [str(self.root), self.kb, cfg['weknora_base_url']],
            'root_identity': signature(self.root)[:2], 'records': {}, 'cache': {},
            'owned_ids': [], 'retire': [], 'events': [], 'seeded': False}
        if self.state['identity'] != [str(self.root), self.kb, cfg['weknora_base_url']]:
            raise ValueError('target_identity_changed_use_new_state_directory')
        self.failure_alerts = FailureAlerts(cfg, clock=clock)
        self.failure_alert_status = {'enabled': self.failure_alerts.enabled}
        self.logs = OperationalLogs(cfg,clock=clock)
        self.logged_index_progress = {}

    def report_failures(self, records=None, *, complete=False, service_error=None):
        try:
            self.failure_alert_status = self.failure_alerts.observe(records,complete=complete,service_error=service_error)
        except Exception as error:
            # A broken desktop permission or logging disk must not change sync decisions.
            self.failure_alert_status = {'enabled': self.failure_alerts.enabled,'logging_error':type(error).__name__}
            print('Failure alert unavailable: '+type(error).__name__,flush=True)
        return self.failure_alert_status

    def persist(self):
        save(self.file, self.state)

    def in_scope_folder(self, value):
        value = (value or '').strip('/')
        return value == self.folder or value.startswith(self.folder + '/')

    def event(self, kind, **details):
        self.state['events'].append({'time': self.clock(), 'kind': kind, **details})
        self.state['events'] = self.state['events'][-1000:]
        if not self.logs.enabled:return
        try:self.log_event(kind,details)
        except Exception as error:
            self.logs.last_error=type(error).__name__
            print('Operational log unavailable: '+type(error).__name__,flush=True)

    def log_event(self, kind, details):
        record=self.state['records'].get(details.get('source_id'),{})
        pending=record.get('pending') or {}
        path=details.get('path') or record.get('desired_path') or record.get('path')
        digest=details.get('source_hash') or pending.get('source_hash') or record.get('latest_observed_hash') or record.get('source_hash')
        fields={'path':path,'source_hash':digest,'previous_source_hash':record.get('source_hash') if record.get('source_hash')!=digest else None,
                'markdown_hash':(pending.get('artifact_sha') or record.get('md_sha')) if pending.get('ingest_mode',record.get('ingest_mode')) != 'original' else None,
                'ingest_mode':pending.get('ingest_mode',record.get('ingest_mode','markdown')),
                'knowledge_ids':pending.get('remote_ids',[]) if pending else record.get('remote_ids',[]),
                'knowledge_base_id':self.kb,**details}
        if 'old' in details:fields['old_path']=details['old']
        if 'new' in details:fields['new_path']=details['new']
        if details.get('source_id') and path and digest:
            generation=record.get('parse_retry_generation',0)
            fields['job_id']=hashlib.sha256((details['source_id']+path+digest+(':retry:'+str(generation) if generation else '')).encode()).hexdigest()
        report=record.get('latest_parse_report_path')
        if 'report_ref' not in fields and report and Path(report).resolve().is_relative_to(self.home):
            fields['report_ref']=str(Path(report).resolve().relative_to(self.home))
        self.logs.record(kind,fields,level='ERROR' if kind in ('parse_failure','source_error') else 'INFO')

    def checked_parse_checkpoint(self, record, path, expected_sha=None):
        path=Path(path).resolve()
        if not path.is_relative_to((self.home/'jobs').resolve()) or path.name!='parse-report.json':
            raise ValueError('parse_checkpoint_outside_managed_jobs')
        meta=hashes(path)
        if expected_sha and meta['sha256']!=expected_sha:
            raise ValueError('parse_checkpoint_changed')
        report=json.loads(path.read_text())
        source=self.source_path(record.get('desired_path') or record['path'])
        source_hash=hashes(source)['sha256']
        supported=source.suffix.lower()=='.xlsx' or (source.suffix.lower() in ('.docx','.pdf','.jpg','.jpeg','.png','.webp')
            and report.get('failure',{}).get('code') in ('UNIT_COVERAGE_INCOMPLETE','MISSING_BODY','IMAGE_ASSETS_UNSUPPORTED')
            and type(report.get('expected_units')) is int and report['expected_units']>0) or (source.suffix.lower()=='.pdf'
            and report.get('failure',{}).get('code')=='IMAGE_ASSETS_UNSUPPORTED'
            and report.get('expected_units',0)>0 and report.get('expected_units')==report.get('completed_units'))
        if not supported or report.get('source_hash')!=source_hash or report.get('status')!='incomplete':
            raise ValueError('parse_checkpoint_source_or_status_mismatch')
        snapshot=path.parent/source.name
        original_hash=source_hash
        if self.cfg.get('compression_registry'):
            from compression_versions import parsing_source
            _,original_hash,_=parsing_source(self.cfg,source,record.get('desired_path') or record['path'],source_hash)
        if report.get('original_source_hash',original_hash)!=original_hash:
            raise ValueError('parse_checkpoint_original_mismatch')
        if not snapshot.is_file() or hashes(snapshot)['sha256']!=original_hash:
            raise ValueError('parse_checkpoint_original_mismatch')
        return {'checkpoint_report':str(path),'checkpoint_sha256':meta['sha256'],'source_hash':source_hash}

    def request_parse_retry(self, identity, checkpoint_report=None):
        record = self.state['records'][identity]
        recovery=self.checked_parse_checkpoint(record,checkpoint_report) if checkpoint_report else None
        if (not record.get('parse_failure') and not record.get('direct_failure') and not recovery) or record['state'] == 'deleted':
            raise ValueError('source_has_no_paused_parse_failure')
        # A request file is consumed by the live writer; never rewrite its in-memory state externally.
        name = hashlib.sha256(identity.encode()).hexdigest() + '.json'
        save(self.home / 'parse-retries' / name, {'source_id': identity,
             'source_hash': record.get('direct_blocked_hash') or record.get('blocked_source_hash'), 'requested_at': self.clock(),**(recovery or {})})

    def apply_parse_retries(self):
        folder = self.home / 'parse-retries'
        for path in folder.glob('*.json'):
            request = json.loads(path.read_text())
            identity = request.get('source_id')
            record = self.state['records'].get(identity)
            recovery=None
            if record and request.get('checkpoint_report'):
                try:
                    recovery=self.checked_parse_checkpoint(record,request['checkpoint_report'],request.get('checkpoint_sha256'))
                except (ValueError,OSError,KeyError):
                    self.event('parse_checkpoint_request_rejected',source_id=identity)
                    self.persist()
                    path.unlink()
                    continue
            if record and record['state'] != 'deleted' and (record.get('direct_failure') and request.get('source_hash') == record.get('direct_blocked_hash') or recovery or record.get('parse_failure') and
                    request.get('source_hash') == record.get('blocked_source_hash')):
                if recovery:
                    record['parse_recovery_checkpoint']=recovery['checkpoint_report']
                record['parse_retry_generation'] = record.get('parse_retry_generation', 0) + 1
                record['parse_retry_requested'] = True
                for name in ('blocked_source_hash', 'blocked_reason', 'blocked_parser_signature',
                             'parse_failure', 'no_text', 'next_attempt','direct_failure','direct_blocked_hash'):
                    record.pop(name, None)
                record['parse_attempts'] = 0
                self.event('parse_retry_requested', source_id=identity)
                self.persist()
            path.unlink()

    def source_path(self, relative):
        path = self.root / relative
        if any(ord(c) < 32 for c in relative) or Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError('source_path_escaped')
        return path

    def md_path(self, relative):
        path = self.markdown / (relative + '.md')
        if any(ord(c) < 32 for c in relative) or Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink() or not path.resolve().is_relative_to(self.markdown):
            raise ValueError('markdown_path_escaped')
        return path

    def seed(self):
        if self.state['seeded']:
            return
        baseline = json.loads(Path(self.cfg['baseline_mapping']).read_text())
        for row in baseline:
            relative = row['source_relative']
            identity = hashlib.sha256(relative.encode()).hexdigest()[:24]
            src = row.get('source') or {}
            if not src.get('sha256'):
                continue
            record = {'path': relative, 'source_hash': src['sha256'], 'md_sha': row['markdown']['sha256'],
                'md_path': row['markdown']['path'], 'remote_ids': row['weknora_ids'],
                'state': no_text_state(self.cfg) if row['action'] == 'transcription_hold' else 'active',
                'no_text': row['action'] == 'transcription_hold',
                'provenance': 'adopted_three_way_verified_baseline', 'missing_scans': 0}
            md = Path(record['md_path'])
            if md.exists() and hashes(md)['sha256'] == record['md_sha']:
                cached = self.home / 'artifacts' / (record['md_sha'] + '.md')
                cached.parent.mkdir(parents=True, exist_ok=True)
                if not cached.exists():
                    cached.write_bytes(md.read_bytes())
                record['cached_md'] = str(cached)
            self.state['records'][identity] = record
            for doc in row['weknora_matches']:
                if self.in_scope_folder(doc.get('folder_path')):
                    self.state['owned_ids'].append(doc['id'])
            source = self.source_path(relative)
            if source.exists() and source.stat().st_size == src.get('size') and source.stat().st_mtime_ns == src.get('mtime_ns'):
                self.state['cache'][relative] = {'sig': signature(source), 'sha256': src['sha256'],
                    'since': self.clock() - self.cfg.get('stable_seconds', 30), 'observations': 2}
        self.state['owned_ids'] = sorted(set(self.state['owned_ids']))
        self.state['seeded'] = True
        self.event('baseline_adopted', sources=len(self.state['records']))
        self.persist()

    def inventory(self):
        if not self.root.is_dir() or signature(self.root)[:2] != self.state['root_identity']:
            raise ValueError('source_root_identity_changed_protective_pause')
        cache = self.state['cache']
        present, placeholders = {}, []
        for path in self.root.rglob('*'):
            if path.is_symlink():
                continue
            if not path.is_file():
                continue
            relative = normalized(path.relative_to(self.root))
            if path.suffix == '.icloud':
                placeholders.append(relative)
                continue
            if any(part.startswith('.') for part in path.relative_to(self.root).parts):
                continue
            sig = signature(path)
            prior = cache.get(relative)
            if not prior or prior['sig'] != sig:
                prior = {'sig': sig, 'since': self.clock(), 'observations': 1}
            else:
                prior['observations'] += 1
            # Replica files have already passed client stability and receiver
            # hash checks. Delaying them here would retire a rename's old path
            # before its committed new path becomes eligible for matching.
            prior['ready'] = bool(self.replica) or (prior['observations'] >= 2 and
                self.clock() - prior['since'] >= self.cfg.get('stable_seconds', 30))
            cache[relative] = prior
            present[relative] = prior
        if self.replica and set(present) - set(self.replica['files']):
            raise ProtocolError('uncommitted_replica_source_file')
        known = [r for r in self.state['records'].values() if r['state'] != 'deleted']
        approved = self.bulk_missing_approvals(present)
        missing = sum((r.get('desired_path') or r['path']) not in present and i not in approved and
                      not (self.replica and deletion_receipt(self.replica, r.get('desired_path') or r['path']))
                      for i,r in self.state['records'].items() if r['state']!='deleted')
        if self.cfg.get('mass_missing_protection_enabled', True) and known and missing > max(self.cfg.get('mass_missing_floor', 50), len(known) * self.cfg.get('mass_missing_fraction', .05)):
            raise ValueError('mass_source_missing_protective_pause')
        return present, placeholders

    def bulk_missing_approvals(self, present):
        """Operator-confirmed paths only; reappearance permanently revokes a grant."""
        path=self.home/'deletion-confirmations.json'
        if self.replica or not path.exists():
            return set()
        receipts=json.loads(path.read_text())
        approved=set()
        changed=False
        for identity,proof in list(receipts.items()):
            if proof.get('evidence')!='operator_confirmed_bulk_cloud_deletion':
                continue
            relative=proof.get('path')
            if relative in present:
                del receipts[identity]
                changed=True
                continue
            record=self.state['records'].get(identity)
            if (record and record['state']!='deleted' and proof.get('confirmed') is True
                    and proof.get('expires_at',0)>self.clock()
                    and proof.get('source_root')==str(self.root)
                    and proof.get('root_identity')==self.state['root_identity']
                    and proof.get('knowledge_base_id')==self.kb
                    and relative==(record.get('desired_path') or record['path'])
                    and proof.get('source_hash')==record.get('source_hash')
                    and proof.get('pending_source_hash')==(record.get('pending') or {}).get('source_hash')):
                approved.add(identity)
        if changed:
            save(path,receipts)
        return approved

    def confirm_missing_batch(self, manifest=None):
        """Explicit operator action; does not claim independent cloud verification."""
        if self.cfg.get('source_mode','local')!='local':
            raise ValueError('bulk_confirmation_requires_local_source')
        allowed=None
        if manifest:
            names=json.loads(Path(manifest).read_text()).get('missing')
            if not isinstance(names,list) or not all(isinstance(n,str) for n in names):
                raise ValueError('invalid_missing_manifest')
            allowed=set(names)
        with (self.home/'worker.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if self.file.exists():
                self.state=json.loads(self.file.read_text())
            if not self.root.is_dir() or signature(self.root)[:2]!=self.state['root_identity']:
                raise ValueError('source_root_identity_changed_protective_pause')
            now=self.clock()
            path=self.home/'deletion-confirmations.json'
            receipts=json.loads(path.read_text()) if path.exists() else {}
            if path.exists():
                backup=self.home/'history/bulk-deletion-confirmations'/str(time.time_ns())/'previous.json'
                save(backup,receipts)
            count=0
            for identity,record in self.state['records'].items():
                relative=record.get('desired_path') or record['path']
                if allowed is not None and relative not in allowed:
                    continue
                source=self.source_path(relative)
                if record['state']=='deleted' or source.exists():
                    continue
                if source.with_name(source.name+'.icloud').exists() or source.with_name('.'+source.name+'.icloud').exists():
                    continue
                receipts[identity]={'confirmed':True,'source_hash':record.get('source_hash'),
                    'pending_source_hash':(record.get('pending') or {}).get('source_hash'),
                    'path':relative,'source_root':str(self.root),'root_identity':self.state['root_identity'],
                    'knowledge_base_id':self.kb,'confirmed_at':now,
                    'expires_at':now+86400,'evidence':'operator_confirmed_bulk_cloud_deletion'}
                count+=1
            save(path,receipts)
            self.event('bulk_cloud_deletions_confirmed',sources=count,code='OPERATOR_CONFIRMED_BULK_CLOUD_DELETION')
            self.persist()
            return {'confirmed_sources':count,'expires_at':now+86400,'mass_missing_threshold_unchanged':True}

    def content_hash(self, relative, observation):
        if not observation['ready']:
            raise ValueError('waiting_for_stable_source')
        if not observation.get('sha256'):
            path = self.source_path(relative)
            before = signature(path)
            digest = hashes(path)['sha256']
            if signature(path) != before or before != observation['sig']:
                raise ValueError('source_changed_during_hash')
            observation['sha256'] = digest
        if self.replica and observation['sha256'] != self.replica['files'][relative]['sha256']:
            raise ProtocolError('replica_source_hash_mismatch')
        return observation['sha256']

    @contextmanager
    def cycle_guard(self):
        if getattr(self, '_owns_lifetime_lock', False):
            with self.source_write_session():
                yield
        else:
            with writer_session(self.cfg):
                with self.source_write_session():
                    yield

    @contextmanager
    def source_write_session(self):
        # The instance lock protects state ownership; this lock permits a compressor
        # to write between cycles without allowing a second state writer to start.
        with (self.home/'source-write.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def cycle(self):
        from docflow.runtime import _STOP
        with self.cycle_guard():
            self.logs.bootstrap(self.state.get('events',[]))
            self.logs.baseline(self.state['records'],self.kb)
            if self.cfg.get('source_mode', 'local') == 'replica':
                self.replica = replica_state(self.cfg)
            self.seed()
            self.report_failures(self.state['records'])
            present, placeholders = self.inventory()
            self.apply_parse_retries()
            placeholder_sources = set()
            for value in placeholders:
                path = Path(value)
                name = path.name.removesuffix('.icloud').removeprefix('.')
                placeholder_sources.add(str(path.with_name(name)))
            docs = self.api.inventory()
            missing = {i: r for i, r in self.state['records'].items()
                       if (r.get('desired_path') or r['path']) not in present and r['state'] != 'deleted'}
            known_paths = {r.get('desired_path') or r['path'] for r in self.state['records'].values() if r['state'] != 'deleted'}
            fresh = {}
            for relative, obs in present.items():
                if relative in known_paths or relative in self.cfg.get('excluded_sources', {}) or Path(relative).suffix.lower() not in SUPPORTED or not obs['ready']:
                    continue
                fresh[relative] = {'sha256': self.content_hash(relative, obs)}
            for identity, relative in rename_pairs(missing, fresh):
                record = self.state['records'][identity]
                record['desired_path'] = relative
                record['state'] = 'rename_pending'
                self.event('rename_detected', source_id=identity, old=record['path'], new=relative)
                fresh.pop(relative)
                missing.pop(identity)
            for relative, obs in fresh.items():
                identity = hashlib.sha256((relative + ':' + str(self.clock())).encode()).hexdigest()[:24]
                self.state['records'][identity] = {'path': relative, 'source_hash': None, 'remote_ids': [], 'state': 'new'}
                self.event('source_discovered', source_id=identity, path=relative, source_hash=obs['sha256'])
            if fresh:
                self.persist()  # Stable source identity must survive the very first model request.
            jobs = 0
            migration_jobs = 0
            for identity, record in self.state['records'].items():
                from docflow.runtime import _STOP
                if _STOP.is_set():break
                if record['state'] == 'deleted':
                    continue
                relative = record.get('desired_path') or record['path']
                if relative in self.cfg.get('excluded_sources', {}):
                    record['state'] = 'excluded_by_policy'
                    continue
                if relative not in present:
                    if relative in placeholder_sources:
                        record.pop('missing_since', None)
                        record.update(state='placeholder_hold', missing_scans=0)
                        continue
                    record.setdefault('missing_since', self.clock())
                    record['missing_scans'] = record.get('missing_scans', 0) + 1
                    record['state'] = 'missing_hold'
                    self.confirmed_deletion(identity, record, docs)
                    continue
                record.pop('missing_since', None)
                record['missing_scans'] = 0
                obs = present[relative]
                if not obs['ready']:
                    record['state'] = 'waiting_stable'
                    continue
                try:
                    digest = self.content_hash(relative, obs)
                    if self.cfg.get('compression_registry'):
                        from compression_versions import adopt_active, verified_receipt
                        if record.get('compression') and record['compression']['output_sha256'] != digest:
                            record.pop('compression')
                        receipt = adopt_active(self.cfg, record, relative, digest, docs)
                        if receipt:
                            self.event('compression_version_adopted', source_id=identity,
                                       original_sha256=receipt['original_sha256'],
                                       output_sha256=digest, cloud_file_id=receipt['cloud_file_id'])
                            self.persist()
                    if (not wants_original(self.cfg,relative) and record['state'] == 'no_text_skipped' and not record.get('parse_retry_requested')
                            and digest == (record.get('blocked_source_hash') or record.get('source_hash'))):
                        continue  # A parser upgrade does not undo the user's skip decision.
                    if record.get('parse_failure') and not wants_original(self.cfg,relative):
                        if self.cfg.get('compression_registry') and record.get('blocked_source_hash') != digest:
                            compressed = verified_receipt(self.cfg, relative, digest)
                            if compressed and compressed['original_sha256'] == record.get('blocked_source_hash'):
                                # Image recompression does not fix an existing parser failure.
                                # Preserve that version's failure; employee edits still retry normally.
                                record.update(blocked_source_hash=digest, compression=compressed)
                        if record.get('blocked_source_hash') == digest and \
                                record.get('blocked_parser_signature') == policy_signature(self.cfg):
                            record['state'] = no_text_state(self.cfg) if record['parse_failure']['category'] == 'no_text' else 'parse_blocked_hold'
                            continue
                        if record.get('blocked_source_hash') == digest:
                            # A new capability version gets a new publication job; old failures stay immutable.
                            previous=record.get('latest_parse_report_path')
                            if previous:
                                try:record['parse_recovery_checkpoint']=self.checked_parse_checkpoint(record,previous)['checkpoint_report']
                                except (ValueError,OSError,KeyError):pass
                            record['parse_retry_generation']=record.get('parse_retry_generation',0)+1
                            record['parse_retry_requested']=True
                        else:
                            record.pop('parse_recovery_checkpoint',None)
                        for name in ('blocked_source_hash', 'blocked_reason', 'blocked_parser_signature',
                                     'parse_failure', 'next_attempt'):
                            record.pop(name, None)
                        record['parse_attempts'] = 0
                    if record.get('parse_retry_hash') not in (None, digest):
                        record.pop('next_attempt', None)
                        record['parse_attempts'] = 0
                    if record.get('next_attempt', 0) > self.clock():
                        if record.get('error') == 'ValueError:compression_cloud_verification_pending':
                            record.pop('next_attempt', None)
                        else:
                            continue
                    terminal = record.get('blocked_source_hash') == digest
                    if terminal and record.get('blocked_reason') == 'model_authorization_required_hold' and not self.cfg.get('allow_model_calls'):
                        record['state'] = 'model_authorization_hold'
                        continue
                    if terminal and record.get('blocked_reason') in ('image_assets_hold', 'direct_local_text_format_unsupported'):
                        record['state'] = 'parse_blocked_hold'
                        continue
                    allowed=self.cfg.get('direct_migration_allowlist')
                    if wants_original(self.cfg,relative) and allowed and relative not in allowed and record.get('ingest_mode') != 'original':
                        record['state']='direct_migration_wait'
                        continue
                    unchanged = digest == record.get('source_hash') and relative == record['path'] and not record.get('parse_retry_requested')
                    unchanged = unchanged and record.get('ingest_mode','markdown') == ('original' if wants_original(self.cfg,relative) else 'markdown')
                    md = Path(record['md_path']) if record.get('md_path') else None
                    if unchanged and md and (not md.exists() or hashes(md)['sha256'] != record.get('md_sha')):
                        unchanged = False
                    if unchanged and not record.get('pending'):
                        # Source is authoritative; keep its already verified parsed version.
                        record['state'] = no_text_state(self.cfg) if record.get('no_text') else 'active'
                        continue
                    if Path(relative).suffix.lower() not in SUPPORTED:
                        record['state'] = 'unsupported_changed_format_hold'
                        record['latest_observed_hash'] = digest
                        continue
                    migrating = wants_original(self.cfg,relative) and record.get('ingest_mode') != 'original'
                    if not record.get('pending') and (migration_jobs >= self.cfg.get('direct_migration_jobs_per_cycle',12) if migrating else jobs >= self.cfg.get('max_jobs_per_cycle',2)):
                        record['state'] = 'queued'
                        continue
                    if migrating:
                        if not record.get('pending'):migration_jobs += 1
                    else:jobs += 1
                    record['latest_observed_hash'] = digest
                    self.process(identity, record, relative, digest, docs)
                except ParseFailure as error:
                    if error.code == 'PARSER_STOPPED':
                        record.update(state='queued', next_attempt=0, latest_parse_report_path=error.report_path)
                        self.event('parse_stopped', source_id=identity, code=error.code)
                        self.persist()
                        break
                    record['latest_parse_report_path'] = error.report_path
                    record['parse_retry_hash'] = digest
                    if error.code != 'RETRY_AFTER':
                        record['parse_attempts'] = record.get('parse_attempts', 0) + 1
                    failure = error.record()
                    if error.retryable and record.get('parse_attempts', 0) < self.cfg.get('parse_retry_limit', 3):
                        record.update(state='parse_retry_wait', last_parse_failure=failure)
                        record['next_attempt'] = max(error.retry_at or 0,
                            self.clock() + min(1800, 30 * 2 ** min(max(0, record.get('parse_attempts', 0) - 1), 6)))
                    else:
                        if error.retryable:
                            failure.update(retryable=False, retry_exhausted=True,
                                           recovery='本版本已用尽自动重试次数，检查环境后定向重试')
                        record.update(state=no_text_state(self.cfg) if error.category == 'no_text' else 'parse_blocked_hold',
                                      parse_failure=failure, last_parse_failure=failure,
                                      blocked_source_hash=digest, blocked_reason=error.code,
                                      blocked_parser_signature=policy_signature(self.cfg), next_attempt=0)
                    self.event('source_skipped_no_text' if record['state'] == 'no_text_skipped' else 'parse_failure', source_id=identity, code=error.code,
                               category=error.category, retryable=failure['retryable'], **{k:v for k,v in failure.get('context',{}).items() if k in ('stage','unit_id','error_type','request_hash','outcome_certainty')})
                    self.persist()
                    self.report_failures({identity:record})
                    continue
                except Exception as error:
                    if str(error) == 'compression_cloud_verification_pending':
                        record.update(state='compression_cloud_hold',
                                      error='compression_cloud_verification_pending', next_attempt=0)
                        self.persist()
                        self.report_failures({identity:record})
                        continue
                    if str(error) in ('model_authorization_required_hold', 'image_assets_hold', 'direct_local_text_format_unsupported'):
                        record.update(state='model_authorization_hold' if str(error) == 'model_authorization_required_hold' else 'parse_blocked_hold',
                                      blocked_source_hash=digest, blocked_reason=str(error), error=str(error))
                        self.event('source_paused',source_id=identity,code=error_code(str(error)))
                        self.persist()
                        self.report_failures({identity:record})
                        continue
                    if str(error) == 'no_readable_text_hold':
                        record.update(state=no_text_state(self.cfg), no_text=True, source_hash=digest,
                                      error='model_reported_no_readable_text', next_attempt=0)
                        self.event('source_skipped_no_text' if record['state'] == 'no_text_skipped' else 'source_paused',source_id=identity,code='NO_READABLE_TEXT')
                        self.persist()
                        self.report_failures({identity:record})
                        continue
                    record.update(state='retry_wait', error=type(error).__name__ + ':' + str(error)[:160],
                        attempts=record.get('attempts', 0) + 1)
                    record['next_attempt'] = self.clock() + min(1800, 30 * 2 ** min(record['attempts'] - 1, 6))
                    self.event('source_error', source_id=identity, error_type=type(error).__name__,code=error_code(record['error']))
                self.persist()
                self.report_failures({identity:record})
            if not _STOP.is_set():self.cleanup_retired()
            self.state['last_scan_complete'] = self.clock()
            self.persist()
            self.report_failures(self.state['records'],complete=True)
            health = {'time': self.clock(), 'status': 'running', 'source_root': str(self.root),
                'local_inventory_count': len(present), 'placeholder_count': len(placeholders),
                'states': dict(collections.Counter(r['state'] for r in self.state['records'].values())),
                'managed_sources': len(self.state['records']), 'retirement_pending': len(self.state['retire']),
                'mass_missing_protection_enabled':self.cfg.get('mass_missing_protection_enabled',True),
                'pdf_auto_compatibility_enabled':self.cfg.get('pdf_auto_compatibility_enabled',True),
                'excluded_sources': self.cfg.get('excluded_sources', {}),
                'cloud_sync_completion_verified': False, 'client_login_verified': False,
                'deletion_rule': (('collector_committed_deletion_receipt' if self.cfg.get('automatic_deletion_enabled')
                                  else 'replica_deletion_disabled') if self.replica else
                    'automatic_after_repeated_complete_missing_scans_and_grace'
                    if self.cfg.get('automatic_deletion_enabled', False)
                    else 'explicit_sync_complete_and_source_deletion_evidence_required'),
                'delete_grace_seconds': self.cfg.get('delete_grace_seconds', 900),
                'parser_policy_version': PARSER_POLICY_VERSION,
                'source_mode': self.cfg.get('source_mode', 'local'),
                'replica_committed_seq': self.replica['last_seq'] if self.replica else None,
                'parse_failure_categories': dict(collections.Counter(
                    r['parse_failure']['category'] for r in self.state['records'].values()
                    if r.get('parse_failure') and r['state'] not in ('deleted', 'no_text_skipped'))),
                'bot_answer_verified': False, 'failure_alerts': self.failure_alert_status,
                'operational_logs':{'enabled':self.logs.enabled,'logging_error':self.logs.last_error}}
            save(self.home / 'health.json', health)
            self.export_mapping()
            return health

    def confirmed_deletion(self, identity, record, docs):
        automatic = self.cfg.get('automatic_deletion_enabled', False)
        replicated = self.cfg.get('source_mode', 'local') == 'replica'
        if replicated:
            if not automatic or not deletion_receipt(self.replica, record.get('desired_path') or record['path']):
                return
        else:
            minimum_scans = max(2, self.cfg.get('delete_missing_scans', 3)) if automatic else 2
            if record['missing_scans'] < minimum_scans or self.clock() - record['missing_since'] < self.cfg.get('delete_grace_seconds', 900):
                return
            if not automatic:
                file = self.home / 'deletion-confirmations.json'
                receipts = json.loads(file.read_text()) if file.exists() else {}
                proof = receipts.get(identity)
                if not proof or not proof.get('confirmed') or proof.get('source_hash') != record['source_hash'] or proof.get('sync_completed_at', 0) < record['missing_since']:
                    return
        if self.source_path(record.get('desired_path') or record['path']).exists():
            return
        pending = record.get('pending') or {}
        if pending.get('phase') in ('upload_intent', 'uncertain') and not pending.get('remote_ids'):
            self.recover_upload(pending, docs)
            if not pending.get('remote_ids'):
                record['state'] = 'deletion_upload_uncertain_hold'
                self.persist()
                return
        self.archive_md(record.get('md_path'), record.get('md_sha'), identity)
        reason = 'collector_deletion_receipt' if replicated else 'automatic_source_absence' if automatic else 'confirmed_source_deleted'
        remote_ids = set(record.get('remote_ids', []) + pending.get('remote_ids', []) + pending.get('superseded_ids', []))
        self.state['retire'] += [{'id': i, 'reason': reason, 'source_id': identity} for i in sorted(remote_ids)]
        self.event('source_deletion_propagated', source_id=identity, automatic=automatic,
            missing_scans=record['missing_scans'], missing_seconds=self.clock() - record['missing_since'],
            remote_ids=sorted(remote_ids),source_hash=pending.get('source_hash') or record.get('source_hash'))
        record.update(state='deleted', remote_ids=[])
        record.pop('pending', None)
        self.persist()

    def archive_md(self, value, expected, identity):
        if not value or not expected:
            return
        path = Path(value)
        if not path.exists():
            return
        if not path.resolve().is_relative_to(self.markdown) or path.is_symlink() or hashes(path)['sha256'] != expected:
            self.event('markdown_modified_hold', source_id=identity)
            return
        target = self.home / 'history' / identity / (expected + '.md')
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(path.read_bytes())
        if hashes(target)['sha256'] != expected:
            raise ValueError('markdown_backup_failed')
        self.event('markdown_archived',source_id=identity,markdown_hash=expected,
                   artifact_ref=str(target.relative_to(self.home)))
        path.unlink()

    def process(self, identity, record, relative, digest, docs):
        if wants_original(self.cfg,relative):
            from original_ingest import process
            return process(self,identity,record,relative,digest,docs)
        recovery_checkpoint=record.get('parse_recovery_checkpoint')
        if record.get('no_text') and digest == record.get('source_hash') and not recovery_checkpoint:
            record.update(path=relative, state=no_text_state(self.cfg))
            record.pop('desired_path', None)
            return
        pending = record.get('pending')
        if pending and (pending['source_hash'] != digest or pending['path'] != relative):
            if pending['phase'] in ('upload_intent', 'uncertain') and not pending.get('remote_ids'):
                self.recover_upload(pending, docs)
                if not pending.get('remote_ids'):
                    record['state'] = 'upload_uncertain_hold'
                    return
            self.state['retire'] += [{'id': i, 'reason': 'superseded_pending', 'source_id': identity}
                                    for i in pending.get('remote_ids', [])+pending.get('superseded_ids',[])]
            record.pop('pending')
            pending = None
        if not pending or recovery_checkpoint:
            if recovery_checkpoint and pending and pending['phase'] in ('uncertain','upload_intent') and not pending.get('remote_ids'):
                self.recover_upload(pending,docs)
                if not pending.get('remote_ids'):
                    record['state']='upload_uncertain_hold'
                    return
            generation = record.get('parse_retry_generation', 0)
            key_input = identity + relative + digest + (':retry:' + str(generation) if generation else '')
            key = hashlib.sha256(key_input.encode()).hexdigest()
            work = self.home / 'jobs' / key
            record['parse_intent'] = {'source_hash': digest, 'path': relative, 'job_id': key,
                'generation': generation, 'parser_signature': policy_signature(self.cfg), 'phase': 'parsing'}
            self.persist()
            self.event('processing_started',source_id=identity,source_hash=digest,path=relative,
                       knowledge_ids=[],previous_knowledge_ids=record.get('remote_ids',[])+(pending or {}).get('remote_ids',[]))
            cached = Path(record['cached_md']) if record.get('cached_md') else None
            parsed_now=False
            if not recovery_checkpoint and digest == record.get('source_hash') and cached and cached.exists() and hashes(cached)['sha256'] == record['md_sha']:
                self.event('artifact_reused',source_id=identity,method='managed_markdown')
                text = cached.read_text()
                old_parent, new_parent = str(Path(record['path']).parent), str(Path(relative).parent)
                text = text.replace('**来源目录：** 项目to咨询 / ' + old_parent,
                                    '**来源目录：** 项目to咨询 / ' + new_parent, 1)
                old_name, new_name = Path(record['path']).name, Path(relative).name
                if text.startswith('# ' + old_name + '\n'):
                    text = '# ' + new_name + '\n' + text[len('# ' + old_name + '\n'):]
                text = text.replace('\n来源：' + old_name + '；', '\n来源：' + new_name + '；')
                work.mkdir(parents=True, exist_ok=True)
                artifact = work / 'publication.md'
                artifact.write_text(text)
                provenance = {'method': 'reuse_managed_markdown_update_source_location', 'source_hash': digest, 'human_review': False}
                previous_provenance = record.get('provenance')
                if isinstance(previous_provenance, dict) and previous_provenance.get('parse_report'):
                    provenance['parse_report'] = previous_provenance['parse_report']
            else:
                self.event('parse_started',source_id=identity,knowledge_ids=[])
                render_cfg = self.cfg
                # Any previous verified incomplete checkpoint is a resume hint, not a different algorithm.
                previous = record.get('latest_parse_report_path')
                if not recovery_checkpoint and previous and Path(previous).is_file():
                    try:
                        proof = self.checked_parse_checkpoint(record, previous)
                        recovery_checkpoint = proof['checkpoint_report']
                    except (ValueError, OSError, KeyError):
                        pass
                previous_report = recovery_checkpoint or record.get('latest_parse_report_path')
                if Path(relative).suffix.lower() == '.xlsx' and previous_report and Path(previous_report).is_file():
                    previous_quality=json.loads(Path(previous_report).read_text())
                    if recovery_checkpoint or previous_quality.get('status')=='incomplete' and previous_quality.get('source_hash')==digest:
                        render_cfg = {**self.cfg, '_xlsx_checkpoint_work': str(Path(previous_report).parent)}
                elif recovery_checkpoint:
                    render_cfg={**self.cfg,'_source_checkpoint_work':str(Path(recovery_checkpoint).parent)}
                artifact, provenance = self.renderer(render_cfg, self.source_path(relative), relative, digest, work)
                parsed_now=True
            record['latest_parse_report_path'] = provenance.get('parse_report_path')
            compatibility=provenance.get('pdf_compatibility')
            if parsed_now and isinstance(compatibility,dict) and compatibility.get('method')=='automatic_lossless_mupdf_rewrite':
                self.event('pdf_compatibility_verified',source_id=identity,code='PDF_COMPATIBILITY_VERIFIED',
                           expected_units=len(compatibility['pages']),completed_units=len(compatibility['pages']))
            meta = hashes(artifact)
            if parsed_now:
                self.event('parse_metrics',source_id=identity,**{k:v for k,v in provenance.get('parse_runtime',{}).items() if k!='recovery_actions'})
                self.event('parse_completed',source_id=identity,method=provenance.get('method'),
                           knowledge_ids=[],markdown_hash=meta['sha256'],
                           **{k:v for k,v in provenance.get('parse_report',{}).items() if k in ('expected_units','completed_units','missing_body_units')})
            prior_pending=pending
            pending = {'path': relative, 'source_hash': digest, 'artifact': str(artifact), 'artifact_sha': meta['sha256'],
                'md5': meta['md5'], 'size': meta['size'], 'phase': 'prepared', 'remote_ids': [], 'provenance': provenance}
            if recovery_checkpoint and prior_pending:
                pending['supersedes_pending']=prior_pending
                pending['superseded_ids']=list(dict.fromkeys(prior_pending.get('remote_ids',[])+prior_pending.get('superseded_ids',[])))
            record['pending'] = pending
            record.pop('parse_intent', None)
            record.pop('parse_recovery_checkpoint',None)
            self.persist()
            self.event('artifact_prepared',source_id=identity)
        if hashes(Path(pending['artifact']))['sha256'] != pending['artifact_sha']:
            raise ValueError('prepared_markdown_changed')
        if hashes(self.source_path(relative))['sha256'] != digest:
            raise ValueError('source_changed_before_publish')
        if pending['phase'] in ('uncertain', 'upload_intent'):
            self.recover_upload(pending, docs)
            if not pending['remote_ids']:
                record['state'] = 'upload_uncertain_hold'
                return
        if pending['phase'] == 'prepared':
            hits = [d for d in docs if d.get('file_hash') == pending['md5'] and d.get('file_size') == pending['size'] and d.get('file_type') == 'md']
            if hits:
                pending.update(remote_ids=[hits[0]['id']], phase='learning')
                self.event('upload_deduplicated',source_id=identity)
            else:
                pending['phase'] = 'upload_intent'
                self.persist()
                self.event('upload_started',source_id=identity)
                metadata = {'sync_scope': 'project-consultation', 'sync_source_relative': relative,
                            'sync_source_sha256': digest, 'sync_artifact_sha256': pending['artifact_sha']}
                body, headers = multipart(Path(pending['artifact']), self.folder + '/' + relative + '.md', metadata)
                try:
                    status, response = self.api.request('/knowledge-bases/' + self.kb + '/knowledge/file', body, headers)
                except Exception:
                    pending['phase'] = 'uncertain'
                    self.persist()
                    raise
                doc = response.get('data') or {}
                if status not in (200, 201, 409) or not doc.get('id'):
                    pending['phase'] = 'prepared' if status in (400, 401, 403, 413, 429) else 'uncertain'
                    self.persist()
                    raise ValueError('upload_rejected_' + str(status))
                if doc.get('file_hash') != pending['md5'] or doc.get('file_size') != pending['size']:
                    pending['phase'] = 'uncertain'
                    self.persist()
                    raise ValueError('upload_response_hash_mismatch')
                pending.update(remote_ids=[doc['id']], phase='learning')
                if status != 409:
                    self.state['owned_ids'].append(doc['id'])
                self.persist()
                self.event('upload_accepted',source_id=identity,http_status=status)
        identity_remote = pending['remote_ids'][0]
        status, response = self.api.request('/knowledge/' + identity_remote)
        doc = response.get('data') or {}
        if status != 200:
            raise ValueError('remote_status_unavailable')
        if doc.get('parse_status') == 'failed':
            record['state'] = 'platform_failed_hold'
            record['error'] = 'platform_parse_failed'
            return
        if doc.get('parse_status') != 'completed' or doc.get('enable_status') != 'enabled' or doc.get('pending_subtasks_count'):
            progress=[doc.get('parse_status'),doc.get('pending_subtasks_count')]
            progress_key=(identity,digest,identity_remote)
            if self.logged_index_progress.get(progress_key)!=progress:
                self.event('index_waiting',source_id=identity,parse_status=progress[0],pending_subtasks_count=progress[1])
                if len(self.logged_index_progress)>10000:self.logged_index_progress.clear()
                self.logged_index_progress[progress_key]=progress
            record['state'] = 'learning'
            return
        if not self.index_probe(identity_remote):
            raise ValueError('retrieval_probe_failed')
        self.event('index_and_retrieval_verified',source_id=identity)
        self.logged_index_progress.pop((identity,digest,identity_remote),None)
        desired_folder = (self.folder + '/' + str(Path(relative).parent)).rstrip('/.')
        if (doc.get('folder_path') or '') != desired_folder:
            if references(self.state['records'], identity_remote) == 1 and identity_remote in self.state['owned_ids']:
                status, response = self.api.request('/knowledge/folder', json.dumps({
                    'kb_id': self.kb, 'knowledge_ids': [identity_remote], 'folder_path': desired_folder}).encode(),
                    {'Content-Type': 'application/json'})
                if status != 200 or not response.get('success'):
                    raise ValueError('directory_alignment_failed')
                status, response = self.api.request('/knowledge/' + identity_remote)
                if status != 200 or response['data'].get('folder_path') != desired_folder:
                    raise ValueError('directory_alignment_not_verified')
            else:
                record['shared_folder_alias'] = True
        if hashes(self.source_path(relative))['sha256'] != digest:
            raise ValueError('source_changed_after_indexing')
        target = self.md_path(relative)
        old_path, old_sha = record.get('md_path'), record.get('md_sha')
        if target.exists() and hashes(target)['sha256'] != pending['artifact_sha']:
            target_hash = hashes(target)['sha256']
            history = self.home / 'history' / identity / (target_hash + '.md')
            history.parent.mkdir(parents=True, exist_ok=True)
            if not history.exists():
                history.write_bytes(target.read_bytes())
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + '.robot5.tmp')
        temp.write_bytes(Path(pending['artifact']).read_bytes())
        if hashes(temp)['sha256'] != pending['artifact_sha']:
            raise ValueError('markdown_copy_mismatch')
        os.replace(temp, target)
        previous = list(set(record.get('remote_ids', [])+pending.get('superseded_ids',[])))
        previous_source_hash = record.get('source_hash')
        self.state['retire'] += [{'id': i, 'reason': 'new_version_verified', 'source_id': identity}
                                for i in previous if i not in pending['remote_ids']]
        record.update(path=relative, source_hash=digest, ingest_mode='markdown', md_sha=pending['artifact_sha'], md_path=str(target),
            remote_ids=pending['remote_ids'], state='active', provenance=pending['provenance'],
            keyword_retrieval_verified=True, next_attempt=0, attempts=0, no_text=False)
        cached = self.home / 'artifacts' / (pending['artifact_sha'] + '.md')
        cached.parent.mkdir(parents=True, exist_ok=True)
        if not cached.exists():
            cached.write_bytes(Path(pending['artifact']).read_bytes())
        record['cached_md'] = str(cached)
        record.pop('error', None)
        record.pop('desired_path', None)
        record.pop('pending', None)
        for name in ('parse_failure', 'last_parse_failure', 'parse_retry_hash', 'parse_retry_requested',
                     'blocked_source_hash', 'blocked_reason', 'blocked_parser_signature'):
            record.pop(name, None)
        record['parse_attempts'] = 0
        self.event('version_activated', source_id=identity, path=relative, remote_ids=record['remote_ids'],
                   previous_knowledge_ids=previous,previous_source_hash=previous_source_hash)
        self.persist()
        if old_path and old_path != str(target):
            self.archive_md(old_path, old_sha, identity)

    def recover_upload(self, pending, docs):
        types = {'xlsx','xls','csv'} if pending.get('ingest_mode') == 'original' else {'md'}
        hits = [d for d in docs if d.get('file_hash') == pending['md5'] and d.get('file_size') == pending['size'] and d.get('file_type') in types]
        if hits:
            pending.update(remote_ids=[hits[0]['id']], phase='learning')
            # Own only new intended entries within this managed scope.
            if self.in_scope_folder(hits[0].get('folder_path')):
                self.state['owned_ids'].append(hits[0]['id'])
            self.persist()

    def index_probe(self, identity):
        status, response = self.api.request('/chunks/' + identity + '?page=1&page_size=100')
        if status != 200:
            return False
        chunk = next((c for c in response.get('data', []) if c.get('chunk_type') == 'text'
                      and c.get('is_enabled') and c.get('content', '').strip()), None)
        if not chunk:
            return False
        body = {'query_text': chunk['content'][:160], 'knowledge_ids': [identity], 'match_count': 5,
                'disable_vector_match': True, 'vector_threshold': 0, 'keyword_threshold': 0,
                'skip_context_enrichment': True}
        status, response = self.api.request('/knowledge-bases/' + self.kb + '/hybrid-search',
            json.dumps(body, ensure_ascii=False).encode(), {'Content-Type': 'application/json'})
        return status == 200 and any(h.get('knowledge_id') == identity for h in response.get('data', []))

    def cleanup_retired(self):
        kept = []
        for job in self.state['retire']:
            identity = job['id']
            if references(self.state['records'], identity):
                continue  # Another retained source still owns the shared entry.
            if identity not in self.state['owned_ids']:
                kept.append(dict(job, status='not_owned_hold'))
                continue
            try:
                status, response = self.api.request('/knowledge/' + identity)
                if status == 404:
                    continue
                doc = response.get('data') or {}
                if status != 200 or doc.get('knowledge_base_id') != self.kb or not self.in_scope_folder(doc.get('folder_path')):
                    kept.append(dict(job, status='target_scope_hold'))
                    continue
                backup = self.home / 'retired-backups' / identity
                backup.mkdir(parents=True, exist_ok=True)
                suffix = doc.get('file_type') if doc.get('file_type') in ('xlsx','xls','csv') else 'md'
                raw_path = backup / ('original.'+suffix)
                if not raw_path.exists():
                    req = urllib.request.Request(self.api.base + '/knowledge/' + identity + '/download', headers=self.api.auth)
                    with self.api.opener.open(req, timeout=90) as response_file:
                        raw_path.write_bytes(response_file.read())
                if hashes(raw_path)['md5'] != doc['file_hash']:
                    raise ValueError('retirement_backup_hash_mismatch')
                save(backup / 'metadata.json', {k: doc.get(k) for k in ('id', 'knowledge_base_id', 'file_name', 'file_hash', 'file_size', 'folder_path')})
                status, response = self.api.request('/knowledge/' + identity, method='DELETE')
                if status not in (200, 202, 204, 404):
                    raise ValueError('retirement_rejected')
                self.event('retirement_submitted', source_id=job.get('source_id'),remote_id=identity, reason=job['reason'])
                status, _ = self.api.request('/knowledge/' + identity)
                if status != 404:
                    kept.append(dict(job, status='retirement_pending'))
                else:
                    self.event('retirement_verified',source_id=job.get('source_id'),remote_id=identity)
            except Exception as error:
                kept.append(dict(job, status='retirement_retry', error=type(error).__name__))
        self.state['retire'] = kept

    def export_mapping(self):
        rows = [{'source_id': i, 'source_relative': r['path'], 'desired_path': r.get('desired_path'),
                 'source_sha256': r.get('source_hash'), 'ingest_mode':r.get('ingest_mode','markdown'),
                 'original_artifact':r.get('original_artifact'),'artifact_sha256':r.get('artifact_sha256'),
                 'markdown': r.get('md_path'),
                 'markdown_sha256': r.get('md_sha'), 'weknora_ids': r.get('remote_ids', []),
                 'state': r['state'], 'pending': r.get('pending'), 'error': r.get('error'),
                 'parse_failure': r.get('parse_failure') or r.get('last_parse_failure'),
                 'compression': r.get('compression'),
                 'parse_report_path': r.get('latest_parse_report_path')}
                for i, r in self.state['records'].items()]
        save(self.home / 'mapping.json', rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['run', 'watch', 'status', 'alerts', 'trace', 'export-logs', 'confirm-delete', 'confirm-missing-batch', 'retry-parse','scan-now'])
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--source-id')
    parser.add_argument('--checkpoint-report',type=Path)
    parser.add_argument('--source-hash')
    parser.add_argument('--source-relative')
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--missing-manifest',type=Path)
    parser.add_argument('--sync-complete', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    cfg = json.loads(args.config.read_text())
    if args.operation in ('trace','export-logs'):
        logs=OperationalLogs(cfg)
        if args.operation=='trace':
            print(json.dumps(logs.query(args.source_id,args.source_relative,args.source_hash),ensure_ascii=False,indent=2))
        else:
            if not args.destination:raise ValueError('support_log_destination_required')
            print(str(logs.export(args.destination)))
        return
    if args.operation == 'alerts':
        path = Path(cfg['state_dir'])/'failure-alerts/current.json'
        print(path.read_text() if path.exists() else '{"status":"no_failure_log_yet"}')
        return
    if args.operation == 'status':
        path = Path(cfg['state_dir']) / 'health.json'
        print(path.read_text() if path.exists() else '{"status":"not_started"}')
        return
    if args.operation in ('run', 'watch'):
        try:
            with writer_session(cfg):
                return execute(cfg, args, owns_lock=True)
        except BlockingIOError:
            print('已有同步writer运行；本次未读取状态或启动第二个循环。', flush=True)
            return 2
    return execute(cfg, args)


def execute(cfg, args, owns_lock=False):
    from docflow.runtime import reset_stop, request_stop
    if owns_lock:reset_stop()
    worker = Sync(cfg)
    worker._owns_lifetime_lock = owns_lock
    if args.operation == 'scan-now':
        save(worker.home/'scan-now.json',{'requested_at':time.time()})
        print('已请求现有writer尽快扫描，稳定等待与删除规则保持不变。')
        return
    if args.operation == 'confirm-missing-batch':
        print(json.dumps(worker.confirm_missing_batch(args.missing_manifest),ensure_ascii=False))
        return
    if args.operation == 'retry-parse':
        if not args.source_id:
            raise ValueError('source_id_required')
        worker.request_parse_retry(args.source_id,args.checkpoint_report)
        print('已提交定向解析重试，后台服务将在下一轮处理。')
        return
    if args.operation == 'confirm-delete':
        if not args.sync_complete or not args.source_id or not args.source_hash:
            raise ValueError('explicit_sync_complete_and_source_identity_required')
        with (worker.home / 'operator-requests.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            record = worker.state['records'][args.source_id]
            if worker.source_path(record['path']).exists() or record['source_hash'] != args.source_hash:
                raise ValueError('deletion_evidence_mismatch')
            file = worker.home / 'deletion-confirmations.json'
            receipts = json.loads(file.read_text()) if file.exists() else {}
            receipts[args.source_id] = {'confirmed': True, 'source_hash': args.source_hash,
                'sync_completed_at': time.time(), 'evidence': 'operator_confirmed_client_sync_completed_and_source_deleted'}
            save(file, receipts)
        return
    stop = False

    def stopping(*_):
        nonlocal stop
        stop = True
        request_stop()

    signal.signal(signal.SIGINT, stopping)
    signal.signal(signal.SIGTERM, stopping)
    if owns_lock:
        save(worker.home/'worker-ready.json',{'pid':os.getpid(),'source_write_lock_protocol':1,'parser_policy_version':PARSER_POLICY_VERSION,
            'parser_signature':policy_signature(cfg),'state_sha256':hashes(worker.file)['sha256'] if worker.file.exists() else None,
            'record_count':len(worker.state['records']),'ready_at':time.time()})
    while not stop:
        if (worker.home/'deployment-hold.json').exists():
            time.sleep(.2)
            continue
        try:
            health=worker.cycle()
            if worker.logs.enabled:
                summary={k:health[k] for k in ('time','status','states','retirement_pending')}
                worker.logs.record('cycle_completed',{'status':health['status'],'states':health['states'],
                    'record_count':health['managed_sources'],'retirement_pending':health['retirement_pending']},runtime=True)
                print(json.dumps(summary,ensure_ascii=False),flush=True)
            else:
                print(json.dumps(health, ensure_ascii=False), flush=True)
        except Exception as error:
            if not isinstance(error,BlockingIOError):
                worker.report_failures(service_error=error)
            worker.logs.record('cycle_failed',{'code':error_code(type(error).__name__+':'+str(error)),
                                              'error_type':type(error).__name__},level='ERROR')
            save(worker.home / 'health.json', {'time': time.time(), 'status': 'protective_pause',
                'error': type(error).__name__ + ':' + error_code(type(error).__name__+':'+str(error)), 'existing_versions_preserved': True,
                'failure_alerts': worker.failure_alert_status})
            print('Cycle error:', type(error).__name__, flush=True)
        worker.logs.maintain()
        if args.operation == 'run':
            break
        for _ in range(cfg.get('interval_seconds', 60)):
            wake=worker.home/'scan-now.json'
            if wake.exists():
                wake.unlink()
                break
            if stop:
                break
            time.sleep(1)

    if stop:
        worker.event('worker_drained',stop_reason='signal')
        worker.persist()


if __name__ == '__main__':
    sys.exit(main())
