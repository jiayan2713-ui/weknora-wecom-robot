from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from compression_client import client_status, elapsed_seconds


class ClientChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'client.sqlite'
        with sqlite3.connect(self.database) as connection:
            connection.execute('CREATE TABLE Corp(corpid TEXT)')
            connection.execute('CREATE TABLE Room(name TEXT, space_id TEXT)')
            connection.execute('INSERT INTO Corp VALUES(?)', ('corp-fixture',))
            connection.execute('INSERT INTO Room VALUES(?,?)', ('room-fixture', 'space-fixture'))
        self.cfg = {'client_database': str(self.database), 'room_name': 'room-fixture',
                    'client_corp_id': 'corp-fixture', 'client_space_id': 'space-fixture'}
        self.main = '100 01:00:00 /Applications/企业微信.app/Contents/MacOS/企业微信\n'
        self.node = '200 00:01:00 /Applications/企业微信.app/Contents/Resources/WeDrive/v1/bin/node\n'

    def inspect(self, output):
        with patch('compression_client.subprocess.run', return_value=SimpleNamespace(stdout=output)):
            return client_status(self.cfg)

    def test_missing_or_starting_sync_process_is_not_ready(self):
        self.assertFalse(self.inspect('')['ready'])
        self.assertFalse(self.inspect(self.main)['ready'])
        result = self.inspect(self.main + self.node.replace('00:01:00', '00:00:03'))
        self.assertEqual(result['reason'], 'client_starting')

    def test_identity_checked_without_claiming_server_connectivity(self):
        result = self.inspect(self.main + self.node)
        self.assertTrue(result['ready'])
        self.assertFalse(result['server_connectivity_verified'])

    def test_wrong_cache_identity_is_not_ready(self):
        self.cfg['client_corp_id'] = 'other-corp'
        self.assertFalse(self.inspect(self.main + self.node)['ready'])

    def test_process_query_failure_is_not_ready(self):
        with patch('compression_client.subprocess.run', side_effect=OSError('not available')):
            self.assertFalse(client_status(self.cfg)['ready'])

    def test_elapsed_process_time_with_days(self):
        self.assertEqual(elapsed_seconds('1-02:03:04'), 93784)
        self.assertEqual(elapsed_seconds('02:03'), 123)


if __name__ == '__main__':
    unittest.main()
