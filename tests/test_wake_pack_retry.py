"""A wake from Sleeping or Dormant starts a fresh retry cycle, so it must lift
the single-scraper fallback and let the item try season packs again.

fall_back_to_single_scraper is set by the adding queue when a pack's debrid add
fails, and propagated to every later episode of the same season. That is a
sensible in-cycle escape hatch, but if nothing clears it the item — and the
rest of its season — is banned from pack scraping forever, which starves shows
whose only viable sources are batch packs. These tests pin the contract that a
wake clears the flag and in-cycle moves do not.
"""

import sqlite3
import tempfile
import os
import unittest
from unittest.mock import MagicMock, patch

# database must finish initializing before queues.queue_manager is imported:
# importing queue_manager first re-enters it through database -> routes ->
# queues_routes and dies on a circular import.
from database.database_writing import update_media_item_state
from queues.queue_manager import QueueManager


def _make_manager():
    qm = QueueManager.__new__(QueueManager)  # skip singleton __init__
    qm._move_item_to_queue = MagicMock(return_value=None)
    return qm


_ITEM = {'id': 1, 'type': 'episode', 'title': 'Show', 'imdb_id': 'tt1',
         'season_number': 1, 'episode_number': 2, 'version': 'Anime'}


class WakeClearsSingleScraperFallback(unittest.TestCase):
    def _kwargs_for(self, from_queue):
        qm = _make_manager()
        with patch('database.get_wake_count', return_value=0, create=True):
            qm.move_to_wanted(dict(_ITEM), from_queue)
        self.assertTrue(qm._move_item_to_queue.called)
        return qm._move_item_to_queue.call_args.kwargs

    def test_wake_from_sleeping_reenables_pack_scraping(self):
        self.assertIs(
            self._kwargs_for("Sleeping").get('fall_back_to_single_scraper'),
            False)

    def test_wake_from_dormant_reenables_pack_scraping(self):
        self.assertIs(
            self._kwargs_for("Dormant").get('fall_back_to_single_scraper'),
            False)

    def test_in_cycle_moves_keep_the_fallback_flag(self):
        for from_queue in ("Adding", "Scraping", "Upgrading"):
            self.assertNotIn('fall_back_to_single_scraper',
                             self._kwargs_for(from_queue),
                             f"move from {from_queue} must not touch the flag")

    def test_ghostlisted_item_is_not_moved(self):
        qm = _make_manager()
        with patch('database.get_wake_count', return_value=0, create=True):
            qm.move_to_wanted(dict(_ITEM, ghostlisted=1), "Sleeping")
        qm._move_item_to_queue.assert_not_called()


class UpdateStatePersistsFallbackFlag(unittest.TestCase):
    """update_media_item_state whitelists its optional columns, so a kwarg the
    whitelist is missing gets dropped without error. This round-trip would have
    caught that silent failure mode for fall_back_to_single_scraper."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute('''CREATE TABLE media_items (
            id INTEGER PRIMARY KEY, state TEXT, last_updated TIMESTAMP,
            fall_back_to_single_scraper BOOLEAN DEFAULT 1,
            filled_by_title TEXT, filled_by_magnet TEXT, filled_by_file TEXT,
            filled_by_torrent_id TEXT, scrape_results TEXT, version TEXT,
            resolution TEXT, upgrading_from TEXT, debrid_folder_name TEXT,
            original_filename TEXT, sleep_cycles INTEGER, next_retry_at TIMESTAMP,
            last_scrape_failure TEXT, partial_scrape_sources TEXT,
            partial_scrape_retry_at TIMESTAMP)''')
        conn.execute("INSERT INTO media_items (id, state, fall_back_to_single_scraper) "
                     "VALUES (1, 'Sleeping', 1)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_flag_false_reaches_the_row(self):
        with patch('database.database_writing.get_db_connection',
                   side_effect=self._connect):
            updated = update_media_item_state(
                1, 'Wanted', fall_back_to_single_scraper=False)
        self.assertIsNotNone(updated)
        self.assertEqual(updated['state'], 'Wanted')
        self.assertFalse(updated['fall_back_to_single_scraper'])

    def test_flag_untouched_when_not_passed(self):
        with patch('database.database_writing.get_db_connection',
                   side_effect=self._connect):
            updated = update_media_item_state(1, 'Wanted')
        self.assertIsNotNone(updated)
        self.assertTrue(updated['fall_back_to_single_scraper'])


if __name__ == '__main__':
    unittest.main()
