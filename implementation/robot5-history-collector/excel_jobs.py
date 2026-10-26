"""One background export at a time, without owning the collection lock."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def read_health(root):
    path=Path(root)/'excel-health.json'
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}


def request_export(root,backend='artifact',preview=False,logs=None):
    import collector
    root=Path(root)
    with (root/'excel.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'running'}
        previous=read_health(root)
        if previous.get('next_retry',0)>time.time():
            return {'status':'retry_pending','next_retry':previous['next_retry']}
        result={'status':'queued','requested_at':collector.now(),'backend':backend,'attempts':previous.get('attempts',0)}
        collector.atomic_json(root/'excel-health.json',result)
        # Let the child acquire its own lock; spawning while this descriptor
        # still owns it can make the child exit before the export starts.
        fcntl.flock(lock,fcntl.LOCK_UN)
        command=[sys.executable,str(Path(collector.__file__).resolve()),'export-job',
                 '--root',str(root.resolve()),'--export-backend',backend]
        if preview:command.append('--preview')
        try:
            fd=os.open(root/'export-worker.log',os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
            with os.fdopen(fd,'ab') as stream:
                subprocess.Popen(command,stdout=stream,stderr=stream,start_new_session=True,close_fds=True)
        except OSError as exc:
            result={'status':'failed','checked_at':collector.now(),'error_type':type(exc).__name__,
                    'code':'EXPORT_WORKER_START_FAILED','next_retry':time.time()+60}
            collector.atomic_json(root/'excel-health.json',result)
        if logs:logs.record('history_export_requested',result,level='ERROR' if result['status']=='failed' else 'INFO')
        return result


def run_export(root,backend='artifact',preview=False):
    import collector
    from operational_logs import OperationalLogs,error_fields
    root=Path(root)
    with (root/'excel.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'already_running'}
        previous=read_health(root)
        started=time.monotonic()
        db=None
        logs=None
        try:
            db=collector.connect(root)
            logs=OperationalLogs({'state_dir':str(root),'operational_logging':{'enabled':True}},component='qa_export')
            records,version=collector.capture_export(db,backend)
            result={'status':'running','started_at':collector.now(),'version':version,'records':len(records),'backend':backend}
            collector.atomic_json(root/'excel-health.json',result)
            collector.export_snapshot(db,root,records,version,preview,backend,logs)
            latest=collector.export_version(db,backend)
            result.update(status='success',completed_at=collector.now(),seconds=round(time.monotonic()-started,3),
                          next_retry=0,attempts=0,more_pending=latest!=version)
            collector.atomic_json(root/'excel-health.json',result)
            return result
        except Exception as exc:
            if db:db.rollback()
            attempts=previous.get('attempts',0)+1
            result={'status':'failed','checked_at':collector.now(),'backend':backend,'attempts':attempts,
                    'next_retry':time.time()+min(3600,60*2**min(attempts-1,6)),**error_fields(exc)}
            collector.atomic_json(root/'excel-health.json',result)
            if logs:logs.record('history_export_failed',result,level='ERROR')
            return result
        finally:
            if db:db.close()
