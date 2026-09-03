"""A wake from Sleeping or Dormant starts a fresh retry cycle, so it must lift
the single-scraper fallback and let the item try season packs again.

fall_back_to_single_scraper is set by the adding queue when a pack's debrid add
fails, and propagated to every later episode of the same season. That is a
sensible in-cycle escape hatch, but if nothing clears it the item — and the
rest of its season — is banned from pack scraping forever, which starves shows
whose only viable sources are batch packs. These tests pin the contract that a
wake clears the flag and in-cycle moves do not.
"""

import json
import sqlite3
import tempfile
import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

# database must finish initializing before queues.queue_manager is imported:
# importing queue_manager first re-enters it through database -> routes ->
# queues_routes and dies on a circular import.
from database.database_writing import update_media_item_state
from queues.queue_manager import QueueManager
from scraper.park import PARKS


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

    def _kwargs_for_record(self, from_queue, record):
        qm = _make_manager()
        with patch('database.get_wake_count', return_value=0, create=True):
            qm.move_to_wanted(dict(_ITEM, last_scrape_failure=record), from_queue)
        return qm._move_item_to_queue.call_args.kwargs

    def test_hold_wake_keeps_the_fallback_flag(self):
        # A held rung means the scrape never completed; waking from it is not a
        # fresh cycle, so the pack ban must not be lifted every thirty minutes.
        held = json.dumps({'stage': 'scrape_unavailable', 'holds': 2, 'raw': 0})
        self.assertNotIn('fall_back_to_single_scraper',
                         self._kwargs_for_record("Sleeping", held))

    def test_ladder_wake_clears_the_fallback_flag(self):
        failed = json.dumps({'stage': 'scrape', 'raw': 12, 'passed': 0})
        self.assertIs(self._kwargs_for_record("Sleeping", failed)
                      .get('fall_back_to_single_scraper'), False)

    def test_dormant_wake_clears_even_after_a_hold(self):
        held = json.dumps({'stage': 'scrape_unavailable', 'holds': 8})
        self.assertIs(self._kwargs_for_record("Dormant", held)
                      .get('fall_back_to_single_scraper'), False)

    def test_wake_from_blacklisted_clears_the_fallback_flag(self):
        self.assertIs(self._kwargs_for_record("Blacklisted", None)
                      .get('fall_back_to_single_scraper'), False)

    def test_garbage_failure_record_is_treated_as_a_real_wake(self):
        self.assertIs(self._kwargs_for_record("Sleeping", '{not json')
                      .get('fall_back_to_single_scraper'), False)


class HoldOutlastsAScraperPark(unittest.TestCase):
    """A held rung must not wake the item straight back into a park that
    lasts up to two hours, and held items must not all wake together."""

    def setUp(self):
        for park in PARKS.values():
            park.clear()
        self.addCleanup(lambda: [park.clear() for park in PARKS.values()])

    def _hold(self, item=None):
        qm = QueueManager.__new__(QueueManager)
        qm.move_to_sleeping = MagicMock()
        qm._move_item_to_queue = MagicMock(return_value=None)
        record = json.dumps({'stage': 'scrape_unavailable', 'raw': 0})
        settings = {'retry_unavailable_hold_minutes': 30, 'retry_unavailable_max_holds': 8}
        with patch('queues.queue_manager.get_setting',
                   side_effect=lambda s, k, d=None: settings.get(k, d)):
            before = datetime.now()
            qm.advance_retry_ladder(dict(item or _ITEM), "Scraping",
                                    failure_record=record, hold_rung=True)
        qm.move_to_sleeping.assert_called_once()
        deadline = qm.move_to_sleeping.call_args.kwargs['next_retry_at']
        return qm, (deadline - before).total_seconds() / 60

    def test_plain_hold_is_the_configured_length_plus_jitter(self):
        _, minutes = self._hold()
        self.assertGreaterEqual(minutes, 30)
        self.assertLessEqual(minutes, 30 * 1.2 + 1)

    def test_hold_stretches_to_cover_the_longest_park(self):
        PARKS['Nyaa'].trip()          # 900s = 15 min, shorter than the hold
        PARKS['Torrentio'].trip()     # 120s
        _, minutes = self._hold()
        self.assertGreaterEqual(minutes, 30)
        # Force a park longer than the hold.
        PARKS['Nyaa']._blocked_until = PARKS['Nyaa']._blocked_until + 7200
        _, minutes = self._hold()
        self.assertGreaterEqual(minutes, 135)         # ceil(8100s / 60)
        self.assertLessEqual(minutes, 135 * 1.2 + 1)

    def test_jitter_spreads_deadlines(self):
        seen = {round(self._hold()[1], 3) for _ in range(6)}
        self.assertGreater(len(seen), 1)

    def test_guard_is_keyed_on_collected_at_only(self):
        # filled_by_file alone (an NZB submitted but never collected) must not
        # bounce the item back to Collected.
        qm, _ = self._hold(dict(_ITEM, filled_by_file='release.mkv', collected_at=None))
        for call in qm._move_item_to_queue.call_args_list:
            self.assertNotEqual(call.args[3] if len(call.args) > 3 else None, "Collected")

    def test_collected_row_is_restored_not_laddered(self):
        qm = QueueManager.__new__(QueueManager)
        qm.move_to_sleeping = MagicMock()
        qm._move_item_to_queue = MagicMock(return_value=None)
        with patch('queues.queue_manager.get_setting', return_value=30):
            qm.advance_retry_ladder(dict(_ITEM, collected_at='2026-09-01 00:00:00'), "Scraping")
        qm.move_to_sleeping.assert_not_called()
        self.assertEqual(qm._move_item_to_queue.call_args.args[2:4], ("Collected", "Collected"))


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
        conn.execute("INSERT INTO media_items (id, state, fall_back_to_single_scraper, "
                     "next_retry_at, scrape_results, sleep_cycles) "
                     "VALUES (1, 'Sleeping', 1, '2026-09-02 12:00:00', '[{\"a\":1}]', 3)")
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

    def test_wake_clears_the_stale_deadline_but_keeps_the_rung(self):
        with patch('database.database_writing.get_db_connection',
                   side_effect=self._connect):
            updated = update_media_item_state(1, 'Wanted')
        self.assertIsNone(updated['next_retry_at'])
        self.assertEqual(updated['sleep_cycles'], 3)

    def test_entering_sleeping_drops_scrape_results(self):
        with patch('database.database_writing.get_db_connection',
                   side_effect=self._connect):
            updated = update_media_item_state(1, 'Sleeping')
        self.assertIsNone(updated['scrape_results'])


if __name__ == '__main__':
    unittest.main()
