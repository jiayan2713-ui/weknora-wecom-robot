"""Local acceptance transport and OpenSSH SFTP adapter use the same batch contract."""
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

from replica_protocol import ProtocolError, batch_name, copy_verified, digest, read, validate_manifest, write


class LocalTransport:
    def __init__(self, incoming, fault=None):
        self.incoming = Path(incoming).resolve()
        self.incoming.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fault = fault or (lambda stage, name: None)

    def deliver(self, batch):
        batch = Path(batch)
        manifest = validate_manifest(read(batch / 'manifest.json'))
        folder = self.incoming / batch_name(manifest)
        ready = {'manifest_sha256': digest(manifest)}
        if (folder / 'READY.json').exists():
            if read(folder / 'READY.json') != ready or read(folder / 'manifest.json') != manifest:
                raise ProtocolError('published_batch_immutable')
        else:
            for operation in manifest['upserts']:
                copy_verified(batch / 'objects' / operation['sha256'], folder / 'objects' / operation['sha256'],
                              operation['sha256'], operation['size'])
                self.fault('after_object', operation['path'])
            write(folder / 'manifest.json', manifest)
            self.fault('before_ready', '')
            write(folder / 'READY.json', ready)
        return read(folder / 'ACK.json') if (folder / 'ACK.json').exists() else None


class SFTPTransport:
    def __init__(self, cfg):
        self.cfg = cfg
        self.host, self.user = cfg['host'], cfg['user']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', self.host) or \
                not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', self.user):
            raise ProtocolError('invalid_sftp_destination')
        self.remote = cfg['remote_incoming']
        if not isinstance(self.remote, str) or not self.remote.startswith('/') or \
                '..' in PurePosixPath(self.remote).parts or any(ord(c) < 32 or c in '\\"*?[]' for c in self.remote):
            raise ProtocolError('invalid_sftp_incoming_path')
        self.key = Path(cfg['identity_file']).resolve()
        self.known = Path(cfg['known_hosts_file']).resolve()
        if not self.key.is_file() or self.key.stat().st_mode & 0o077 or not self.known.is_file():
            raise ProtocolError('sftp_credentials_or_host_binding_unavailable')
        port = cfg.get('port', 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ProtocolError('invalid_sftp_port')
        self.command = [cfg.get('sftp_executable', 'sftp'), '-q', '-b', '-', '-P', str(port),
                        '-i', str(self.key), '-oBatchMode=yes', '-oStrictHostKeyChecking=yes',
                        '-oUserKnownHostsFile=' + str(self.known), self.user + '@' + self.host]

    def quote(self, path):
        value = str(path)
        if any(ord(c) < 32 or c in '*?[]' for c in value):
            raise ProtocolError('sftp_path_contains_control_character')
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

    def run(self, commands):
        try:
            result = subprocess.run(self.command, input='\n'.join(commands) + '\n', text=True,
                                    capture_output=True, timeout=self.cfg.get('timeout_seconds', 120))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProtocolError('sftp_unavailable_or_timed_out') from error
        if result.returncode:
            raise ProtocolError('sftp_transfer_failed_exit_' + str(result.returncode))

    def deliver(self, batch):
        batch = Path(batch)
        manifest = validate_manifest(read(batch / 'manifest.json'))
        remote = self.remote.rstrip('/') + '/' + batch_name(manifest)
        with tempfile.TemporaryDirectory(prefix='robot5-sftp-') as temporary:
            temporary = Path(temporary)
            ready_file, ack_file = temporary / 'READY.json', temporary / 'ACK.json'
            self.run(['-get ' + self.quote(remote + '/READY.json') + ' ' + self.quote(ready_file)])
            if ready_file.exists():
                if read(ready_file) != {'manifest_sha256': digest(manifest)}:
                    raise ProtocolError('published_remote_batch_mismatch')
            else:
                commands = ['-mkdir ' + self.quote(remote), '-mkdir ' + self.quote(remote + '/objects')]
                for operation in manifest['upserts']:
                    source = batch / 'objects' / operation['sha256']
                    destination = remote + '/objects/' + operation['sha256']
                    commands += ['put -f ' + self.quote(source) + ' ' + self.quote(destination + '.part'),
                                 '-rm ' + self.quote(destination),
                                 'rename ' + self.quote(destination + '.part') + ' ' + self.quote(destination)]
                for name in ('manifest.json', 'READY.json'):
                    destination = remote + '/' + name
                    commands += ['put -f ' + self.quote(batch / name) + ' ' + self.quote(destination + '.part'),
                                 '-rm ' + self.quote(destination),
                                 'rename ' + self.quote(destination + '.part') + ' ' + self.quote(destination)]
                self.run(commands)
            self.run(['-get ' + self.quote(remote + '/ACK.json') + ' ' + self.quote(ack_file)])
            return read(ack_file) if ack_file.exists() else None


def from_config(cfg):
    if cfg.get('kind') == 'local':
        return LocalTransport(cfg['incoming_dir'])
    if cfg.get('kind') == 'sftp':
        return SFTPTransport(cfg)
    raise ProtocolError('unknown_replica_transport')
