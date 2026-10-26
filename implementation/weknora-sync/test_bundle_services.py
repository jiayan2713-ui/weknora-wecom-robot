from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_mac_bundle as bundle


class BundleServiceTests(unittest.TestCase):
    def test_resume_restores_only_previously_loaded_services(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'journal.json'
            loaded = {label: '/fixture/' + label + '.plist' for label in bundle.COMPRESSION_LABELS}
            bundle.write(journal, {'source_machine': 'fixture', 'stopped': ['WeKnora-app'],
                'worker_was_loaded': True, 'plist': '/fixture/sync.plist',
                'history_was_loaded': True, 'history_plist': '/fixture/history.plist',
                'compression_loaded': loaded})
            calls = []
            with patch.object(bundle, 'machine_id', return_value='fixture'), \
                    patch.object(bundle, 'run', side_effect=lambda args: calls.append(args)), \
                    patch.object(bundle.subprocess, 'run') as status:
                status.return_value.returncode = 1
                bundle.resume_old(journal)
            self.assertEqual(calls[0], ['docker', 'start', 'WeKnora-app'])
            self.assertEqual(len([c for c in calls if 'bootstrap' in c]), 5)
            self.assertEqual(bundle.read(journal)['status'], 'resumed')
            calls.clear()
            with patch.object(bundle, 'machine_id', return_value='another'), patch.object(bundle, 'run') as execute:
                with self.assertRaises(ValueError):
                    bundle.resume_old(journal)
                execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
