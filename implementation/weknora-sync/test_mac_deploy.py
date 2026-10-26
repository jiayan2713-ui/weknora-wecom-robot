import hashlib
import json
import plistlib
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

import mac_deploy
from mac_bundle_crypto import encrypt, unpack


class MacDeployTests(unittest.TestCase):
    def test_restore_waits_for_permanent_postgres_not_temporary_init_server(self):
        from types import SimpleNamespace
        with patch.object(mac_deploy.subprocess,'run',side_effect=[SimpleNamespace(returncode=1),SimpleNamespace(returncode=0)]) as command, \
                patch.object(mac_deploy.time,'sleep') as sleep:
            mac_deploy.wait_postgres('isolated-postgres')
        self.assertEqual(command.call_count,2)
        self.assertIn('cat /proc/1/comm',command.call_args.args[0][-1])
        self.assertEqual(sleep.call_count,1)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.key=self.root/'unlock.key'
        self.key.write_text(secrets.token_hex(32)+'\n')
        self.key.chmod(0o600)
        self.payload=self.root/'payload'
        self.payload.mkdir()
        (self.payload/'private.json').write_text('{"credential":"isolated-fixture"}')
        self.cipher=self.root/'payload.enc'
        self.meta=encrypt(self.payload,self.cipher,self.key,{'source_machine':'source-id'})

    def test_authenticated_round_trip_preserves_private_bytes(self):
        target=self.root/'restored'
        unpack(self.cipher,self.key,self.meta,target)
        self.assertEqual((target/'private.json').read_bytes(),(self.payload/'private.json').read_bytes())
        self.assertEqual((target/'private.json').stat().st_mode & 0o777,0o600)
        self.assertNotIn(b'isolated-fixture',self.cipher.read_bytes())

    def test_setup_rejects_changed_program_before_installing_dependencies(self):
        bundle=self.root/'bundle';bundle.mkdir()
        code=bundle/'app/worker.py';code.parent.mkdir();code.write_text('original')
        mac_deploy.write(bundle/'程序校验.json',{'app/worker.py':hashlib.sha256(code.read_bytes()).hexdigest()})
        metadata={'source_machine':'source-id','program_manifest_sha256':hashlib.sha256((bundle/'程序校验.json').read_bytes()).hexdigest()}
        metadata=encrypt(self.payload,bundle/'payload.enc',self.key,metadata)
        mac_deploy.write(bundle/'manifest.json',metadata)
        code.write_text('changed')
        with patch.object(mac_deploy,'machine_id',return_value='target-id'),patch.object(mac_deploy,'run') as execute:
            with self.assertRaisesRegex(ValueError,'程序文件校验失败'):
                mac_deploy.setup(bundle,self.root/'target',self.key)
            execute.assert_not_called()

    def test_history_service_uses_target_venv_compose_and_private_output(self):
        home=self.root/'installation'
        mac_deploy.write(home/'restore-platform.json',{'status':'restored_channels_disabled'})
        expected={'channel_id':'channel-fixture','agent_id':'agent-fixture'}
        mac_deploy.write(home/'private/history-collector/binding.json',expected)
        calls=[]
        def execute(args,**kwargs):
            calls.append(args)
            if str(args[1]).endswith('migration.py'):
                mac_deploy.write(home/'history-archive/binding.json',{
                    **expected,'compose_file':str((home/'private/platform/compose.json').resolve()),
                    'export_backend':'openpyxl'})
            return b''
        with patch.object(mac_deploy,'run',side_effect=execute), \
                patch.object(Path,'home',return_value=self.root), \
                patch.object(mac_deploy.subprocess,'run') as status:
            status.return_value.returncode=1
            mac_deploy.start_history(home)
        self.assertEqual(calls[0][0],str(home/'.venv/bin/python3'))
        self.assertIn('--compose-file',calls[0])
        self.assertEqual(calls[1][-1],'--full')
        plist=self.root/'Library/LaunchAgents/com.robot5.mac-history-collector.plist'
        data=plistlib.loads(plist.read_bytes())
        self.assertEqual(data['StartInterval'],60)
        self.assertIn(str(home/'history-archive/binding.json'),data['ProgramArguments'])
        self.assertEqual(plist.stat().st_mode & 0o777,0o600)

    def test_wrong_key_tampered_data_and_tampered_metadata_fail_before_extract(self):
        wrong=self.root/'wrong.key'
        wrong.write_text(secrets.token_hex(32))
        with self.assertRaisesRegex(ValueError,'不匹配'):
            unpack(self.cipher,wrong,self.meta,self.root/'wrong')
        self.assertFalse((self.root/'wrong').exists())
        with self.assertRaisesRegex(ValueError,'不匹配'):
            unpack(self.cipher,self.key,{**self.meta,'source_machine':'other'},self.root/'tampered')
        raw=bytearray(self.cipher.read_bytes());raw[-1]^=1;self.cipher.write_bytes(raw)
        with self.assertRaisesRegex(ValueError,'不匹配'):
            unpack(self.cipher,self.key,self.meta,self.root/'tampered-data')

    def test_existing_decryption_directory_is_not_overwritten(self):
        target=self.root/'existing';target.mkdir();(target/'keep').write_text('user data')
        with self.assertRaisesRegex(ValueError,'必须为空'):
            unpack(self.cipher,self.key,self.meta,target)
        self.assertEqual((target/'keep').read_text(),'user data')

    def test_source_mac_cannot_run_setup(self):
        bundle=self.root/'bundle';bundle.mkdir()
        mac_deploy.write(bundle/'manifest.json',{'source_machine':'same-id'})
        with patch.object(mac_deploy,'machine_id',return_value='same-id'):
            with self.assertRaisesRegex(ValueError,'来源Mac'):
                mac_deploy.setup(bundle,self.root/'install',self.key)
        self.assertFalse((self.root/'install').exists())

    def test_predeployment_snapshot_cannot_activate_native_channel(self):
        home=self.root/'home';home.mkdir()
        mac_deploy.write(home/'private/snapshot.json',{'old_services_resumed':True})
        with patch.object(mac_deploy,'run') as command:
            with self.assertRaisesRegex(ValueError,'最终冻结包'):
                mac_deploy.activate(home,True)
            command.assert_not_called()

    def test_bind_rejects_missing_source_before_writing_runtime_state(self):
        home=self.root/'home';home.mkdir()
        mac_deploy.write(home/'private/sync/source-config.json',{})
        mac_deploy.write(home/'private/sync/state.json',{'records':{}})
        mac_deploy.write(home/'private/sync/source-fingerprints.json',{'x.txt':'0'*64})
        source=self.root/'source';source.mkdir()
        with self.assertRaisesRegex(ValueError,'首次对账未通过'):
            mac_deploy.bind(home,source)
        self.assertEqual(mac_deploy.read(home/'source-check.json')['missing'],['x.txt'])
        self.assertFalse((home/'state/state.json').exists())

    def test_bind_preserves_knowledge_ids_pending_artifact_and_disables_deletion(self):
        home=self.root/'home';home.mkdir()
        source=self.root/'source';source.mkdir();(source/'x.txt').write_text('V1')
        checksum=hashlib.sha256((source/'x.txt').read_bytes()).hexdigest()
        old={'state_dir':'/old/state','markdown_root':'/old/markdown','knowledge_base_id':'kb',
             'parser_config':{'model':'existing-model'},'excluded_sources':{'skip.pdf':'preserve'},
             'pdf_compatibility_python':'/old/arm-only/python3'}
        record={'path':'x.txt','state':'learning','source_hash':checksum,'md_path':'/old/markdown/x.txt.md',
                'remote_ids':['knowledge-1'],'missing_since':10,'missing_scans':3,
                'pending':{'artifact':'/old/state/jobs/job/publication.md','provenance':{'parse_report_path':'/old/state/jobs/job/parse-report.json'}}}
        mac_deploy.write(home/'private/sync/source-config.json',old)
        mac_deploy.write(home/'private/sync/state.json',{'records':{'source-1':record},'cache':{'old':1}})
        mac_deploy.write(home/'private/sync/source-fingerprints.json',{'x.txt':checksum})
        (home/'private/sync/markdown').mkdir()
        (home/'private/sync/markdown/x.txt.md').write_text('V1')
        mac_deploy.write(home/'private/sync/failure-alerts/state.json',{'issues':{'source-1':{'fingerprint':'preserved'}}})
        mac_deploy.write(home/'private/sync/logs/mapping-baseline.json',{'record_count':1})
        old['operational_logging']={'enabled':True,'runtime_files':['/old/watch.stdout.log']}
        mac_deploy.write(home/'private/sync/source-config.json',old)
        office=self.root/'soffice';office.touch();poppler=self.root/'pdftoppm';poppler.touch()
        with patch.object(mac_deploy.shutil,'which',side_effect=lambda name:str(office if name=='soffice' else poppler)):
            mac_deploy.bind(home,source)
        cfg=mac_deploy.read(home/'config.json');state=mac_deploy.read(home/'state/state.json')
        restored=state['records']['source-1']
        self.assertEqual(restored['remote_ids'],['knowledge-1'])
        self.assertEqual(restored['pending']['artifact'],str(home/'state/jobs/job/publication.md'))
        self.assertNotIn('missing_since',restored)
        self.assertEqual(restored['missing_scans'],0)
        self.assertFalse(cfg['automatic_deletion_enabled'])
        self.assertEqual(cfg['excluded_sources'],old['excluded_sources'])
        self.assertEqual(cfg['pdf_compatibility_python'],str(home/'.venv/bin/python3'))
        self.assertEqual(state['cache'],{})
        self.assertEqual(mac_deploy.read(home/'state/failure-alerts/state.json')['issues']['source-1']['fingerprint'],'preserved')
        self.assertEqual(mac_deploy.read(home/'state/logs/mapping-baseline.json')['record_count'],1)
        self.assertEqual(cfg['operational_logging']['runtime_files'],[str(home/'watch.stdout.log'),str(home/'watch.stderr.log')])


if __name__=='__main__':unittest.main()
