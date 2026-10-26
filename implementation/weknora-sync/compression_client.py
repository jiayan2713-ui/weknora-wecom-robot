"""Read-only WeCom process/cache checks; never launch or log into the client."""
from pathlib import Path
import sqlite3
import subprocess


class ClientUnavailable(ValueError):
    pass


def elapsed_seconds(value):
    parts = [int(p) for p in value.replace('-', ':').split(':')]
    units = (1, 60, 3600, 86400)
    return sum(v * unit for v, unit in zip(reversed(parts), units))


def client_status(cfg):
    status = {'ready': False, 'reason': 'client_process_check_failed'}
    try:
        completed = subprocess.run(['/bin/ps', '-x', '-o', 'pid=,etime=,comm='],
                                   capture_output=True, text=True, check=True, timeout=10)
        processes = []
        for line in completed.stdout.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) == 3:
                processes.append((fields[0], elapsed_seconds(fields[1]), fields[2]))
        main = [p for p in processes if p[2] == '/Applications/企业微信.app/Contents/MacOS/企业微信']
        nodes = [p for p in processes if p[2].startswith('/Applications/企业微信.app/Contents/Resources/WeDrive/')
                 and p[2].endswith('/bin/node')]
        status.update(main_pids=[p[0] for p in main], sync_pids=[p[0] for p in nodes])
        if not main or not nodes:
            status['reason'] = 'client_not_running' if not main else 'sync_engine_not_running'
            return status
        if min(p[1] for p in nodes) < cfg.get('client_startup_grace_seconds', 20):
            status['reason'] = 'client_starting'
            return status
        # This verifies the configured cache/space identity, not server connectivity.
        database = Path(cfg['client_database']).resolve()
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
            corps = connection.execute('SELECT corpid FROM Corp').fetchall()
            rooms = connection.execute('SELECT space_id FROM Room WHERE name=?', (cfg['room_name'],)).fetchall()
        if (len(corps) != 1 or str(corps[0][0]) != cfg.get('client_corp_id') or
                len(rooms) != 1 or rooms[0][0] != cfg.get('client_space_id')):
            status['reason'] = 'client_cache_identity_mismatch'
            return status
        status.update(ready=True, reason='processes_and_cache_identity_checked',
                      server_connectivity_verified=False)
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        status['error_type'] = type(error).__name__
    return status
