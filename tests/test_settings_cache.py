"""get_setting must not re-parse config.json on every call.

It is called from per-result and per-file loops; each call used to open, lock
and re-parse the file. The parse is cached against the file's stat key and
invalidated explicitly by every in-process writer.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from utilities import settings


class SettingsCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {'USER_CONFIG': self.dir})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)
        settings.invalidate_config_cache()
        self.addCleanup(settings.invalidate_config_cache)
        self._write({'Queue': {'wanted_recent_release_days': 14, 'list': [1, 2]},
                     'Scraping': {'versions': {'Main': {'x': 1}}}})

    def _write(self, config):
        with open(settings.get_config_file_path(), 'w') as f:
            json.dump(config, f)

    def test_second_read_does_not_reparse(self):
        with patch.object(settings.json, 'load', wraps=json.load) as load:
            self.assertEqual(settings.get_setting('Queue', 'wanted_recent_release_days'), 14)
            self.assertEqual(settings.get_setting('Queue', 'wanted_recent_release_days'), 14)
            self.assertEqual(settings.get_setting('Scraping', 'versions')['Main']['x'], 1)
        self.assertEqual(load.call_count, 1)

    def test_external_write_is_picked_up(self):
        settings.get_setting('Queue', 'wanted_recent_release_days')
        time.sleep(0.01)
        self._write({'Queue': {'wanted_recent_release_days': 30}})
        self.assertEqual(settings.get_setting('Queue', 'wanted_recent_release_days'), 30)

    def test_save_config_is_visible_immediately(self):
        config = settings.load_config()
        config['Queue']['wanted_recent_release_days'] = 7
        settings.save_config(config)
        self.assertEqual(settings.get_setting('Queue', 'wanted_recent_release_days'), 7)

    def test_restore_of_an_older_file_is_not_served_stale(self):
        # copy2 preserves the source mtime; a restore can make the file LOOK
        # older than the cached parse. Writers invalidate explicitly.
        older = os.path.join(self.dir, 'old.json')
        with open(older, 'w') as f:
            json.dump({'Queue': {'wanted_recent_release_days': 99}}, f)
        os.utime(older, (time.time() - 3600, time.time() - 3600))
        settings.get_setting('Queue', 'wanted_recent_release_days')
        shutil.copy2(older, settings.get_config_file_path())
        settings.invalidate_config_cache()
        self.assertEqual(settings.get_setting('Queue', 'wanted_recent_release_days'), 99)

    def test_returned_sections_are_copies(self):
        section = settings.get_setting('Queue')
        section['wanted_recent_release_days'] = 1
        section['list'].append(3)
        self.assertEqual(settings.get_setting('Queue', 'wanted_recent_release_days'), 14)
        self.assertEqual(settings.get_setting('Queue', 'list'), [1, 2])
        whole = settings.load_config()
        whole['Queue']['wanted_recent_release_days'] = 2
        self.assertEqual(settings.get_setting('Queue', 'wanted_recent_release_days'), 14)

    def test_missing_file_is_not_cached(self):
        os.unlink(settings.get_config_file_path())
        settings.invalidate_config_cache()
        self.assertEqual(settings.get_setting('Queue', 'wanted_recent_release_days', 5), 5)
        self._write({'Queue': {'wanted_recent_release_days': 8}})
        self.assertEqual(settings.get_setting('Queue', 'wanted_recent_release_days', 5), 8)


if __name__ == '__main__':
    unittest.main()
