"""Exhaustion: the Kth Dormant entry blacklists that one item, and a requeue resets it.

Dormant used to be forever. Every item that no source could fill re-entered
Wanted every week, and the active pool never shrank. Blacklisted already had
everything a terminal state needs, so exhaustion reuses it -- under a strict
contract: one row, never a sibling, never on a transient failure, only after
K full ladders.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from database.database_writing import update_media_item_state
from queues.queue_manager import QueueManager


_ITEM = {'id': 1, 'type': 'episode', 'title': 'Show', 'imdb_id': 'tt1',
         'season_number': 1, 'episode_number': 2, 'version': 'Anime', 'sleep_cycles': 5}


def _manager():
    qm = QueueManager.__new__(QueueManager)
    qm._move_item_to_queue = MagicMock(return_value=None)
    qm.move_to_blacklisted = MagicMock()
    return qm


class KthDormantEntryBlacklists(unittest.TestCase):
    def _dormant(self, cycles_done, k=3):
        qm = _manager()
        with patch('queues.queue_manager.get_setting',
                   side_effect=lambda s, key, d=None: k if key == 'dormant_cycles_before_blacklist' else d), \
                patch('database.update_media_item', create=True) as update:
            qm.move_to_dormant(dict(_ITEM, dormant_cycles=cycles_done), "Scraping",
                               failure_record=json.dumps({'stage': 'scrape', 'raw': 0}))
        return qm, update

    def test_first_entry_goes_dormant_and_counts(self):
        qm, _ = self._dormant(0)
        qm.move_to_blacklisted.assert_not_called()
        args, kwargs = qm._move_item_to_queue.call_args
        self.assertEqual(args[2:4], ("Dormant", "Dormant"))
        self.assertEqual(kwargs['dormant_cycles'], 1)
        self.assertIsNotNone(kwargs['next_retry_at'])

    def test_second_entry_still_dormant(self):
        qm, _ = self._dormant(1)
        qm.move_to_blacklisted.assert_not_called()
        self.assertEqual(qm._move_item_to_queue.call_args.kwargs['dormant_cycles'], 2)

    def test_kth_entry_blacklists_this_item_only(self):
        qm, update = self._dormant(2)
        qm._move_item_to_queue.assert_not_called()
        qm.move_to_blacklisted.assert_called_once()
        item, from_queue = qm.move_to_blacklisted.call_args.args
        self.assertEqual(item['id'], 1)
        self.assertEqual(from_queue, "Scraping")
        record = json.loads(update.call_args.kwargs['last_scrape_failure'])
        self.assertEqual(record['stage'], 'exhausted')
        self.assertEqual(update.call_args.kwargs['dormant_cycles'], 3)

    def test_zero_cap_never_blacklists(self):
        qm, _ = self._dormant(40, k=0)
        qm.move_to_blacklisted.assert_not_called()
        self.assertEqual(qm._move_item_to_queue.call_args.kwargs['dormant_cycles'], 41)

    def test_garbage_counter_is_treated_as_zero(self):
        qm = _manager()
        with patch('queues.queue_manager.get_setting', return_value=3), \
                patch('database.update_media_item', create=True):
            qm.move_to_dormant(dict(_ITEM, dormant_cycles='what'), "Scraping")
        self.assertEqual(qm._move_item_to_queue.call_args.kwargs['dormant_cycles'], 1)

    def test_ghostlisted_item_is_never_touched(self):
        qm = _manager()
        with patch('queues.queue_manager.get_setting', return_value=3):
            qm.move_to_dormant(dict(_ITEM, dormant_cycles=2, ghostlisted=1), "Scraping")
        qm.move_to_blacklisted.assert_not_called()
        qm._move_item_to_queue.assert_not_called()


class LeavingBlacklistedResetsTheLadder(unittest.TestCase):
    def test_wake_from_blacklisted_resets_rung_deadline_and_cycles(self):
        qm = _manager()
        with patch('database.get_wake_count', return_value=0, create=True):
            qm.move_to_wanted(dict(_ITEM, dormant_cycles=3), "Blacklisted")
        kwargs = qm._move_item_to_queue.call_args.kwargs
        self.assertEqual(kwargs['sleep_cycles'], 0)
        self.assertEqual(kwargs['dormant_cycles'], 0)
        self.assertIsNone(kwargs['next_retry_at'])
        self.assertIs(kwargs['fall_back_to_single_scraper'], False)

    def test_wake_from_dormant_keeps_its_count(self):
        qm = _manager()
        with patch('database.get_wake_count', return_value=0, create=True):
            qm.move_to_wanted(dict(_ITEM, dormant_cycles=2), "Dormant")
        kwargs = qm._move_item_to_queue.call_args.kwargs
        self.assertNotIn('dormant_cycles', kwargs)
        self.assertNotIn('sleep_cycles', kwargs)


class CounterPersistence(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute('''CREATE TABLE media_items (
            id INTEGER PRIMARY KEY, state TEXT, last_updated TIMESTAMP,
            fall_back_to_single_scraper BOOLEAN DEFAULT 1, dormant_cycles INTEGER DEFAULT 0,
            filled_by_title TEXT, filled_by_magnet TEXT, filled_by_file TEXT,
            filled_by_torrent_id TEXT, scrape_results TEXT, version TEXT,
            resolution TEXT, upgrading_from TEXT, debrid_folder_name TEXT,
            original_filename TEXT, sleep_cycles INTEGER, next_retry_at TIMESTAMP,
            last_scrape_failure TEXT, partial_scrape_sources TEXT,
            partial_scrape_retry_at TIMESTAMP)''')
        conn.execute("INSERT INTO media_items (id, state, sleep_cycles, dormant_cycles) VALUES (1, 'Sleeping', 5, 2)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_counter_is_written_through_the_whitelist(self):
        with patch('database.database_writing.get_db_connection', side_effect=self._connect):
            updated = update_media_item_state(1, 'Dormant', dormant_cycles=3)
        self.assertEqual(updated['dormant_cycles'], 3)

    def test_a_successful_add_resets_the_counter(self):
        with patch('database.database_writing.get_db_connection', side_effect=self._connect):
            updated = update_media_item_state(1, 'Checking')
        self.assertEqual(updated['dormant_cycles'], 0)
        self.assertEqual(updated['sleep_cycles'], 0)

    def test_dormant_wake_keeps_the_counter(self):
        with patch('database.database_writing.get_db_connection', side_effect=self._connect):
            updated = update_media_item_state(1, 'Wanted')
        self.assertEqual(updated['dormant_cycles'], 2)


if __name__ == '__main__':
    unittest.main()
