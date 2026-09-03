"""A season pack may fill every waiting sibling, wherever the ladder parked it.

The Adding queue used to hand find_related_items a snapshot of the in-memory
Wanted and Scraping queues. The retry ladder parks most unfilled episodes in
Sleeping/Dormant (and, once exhausted, Blacklisted), so a pack filled only the
few siblings that were Wanted at that instant and the rest re-added the same
pack later. A row a PERSON blacklisted is never filled.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from database.database_writing import update_media_item_state  # import order: database first
from queues.states import (RELATED_FILL_STATES, is_exhausted_blacklist,
                           is_fillable_by_pack, failure_stage)
from queues.adding_queue import _related_fill_candidates
from queues.queue_manager import QueueManager


EXHAUSTED = json.dumps({'stage': 'exhausted', 'error': '3 Dormant cycles'})
SCRAPE = json.dumps({'stage': 'scrape', 'raw': 0})


def _row(state, record=None, **extra):
    row = {'id': extra.pop('id', 1), 'state': state, 'imdb_id': 'tt1', 'type': 'episode',
           'season_number': 1, 'episode_number': 2, 'version': 'Anime',
           'last_scrape_failure': record}
    row.update(extra)
    return row


class Eligibility(unittest.TestCase):
    def test_ladder_states_are_fillable(self):
        for state in ('Wanted', 'Scraping', 'Sleeping', 'Dormant'):
            with self.subTest(state=state):
                self.assertTrue(is_fillable_by_pack(_row(state)))

    def test_terminal_and_active_states_are_not(self):
        for state in ('Collected', 'Upgrading', 'Checking', 'Adding', 'Unreleased'):
            with self.subTest(state=state):
                self.assertFalse(is_fillable_by_pack(_row(state)))

    def test_exhausted_blacklist_is_fillable_when_restorable(self):
        with patch('database.blacklist.is_restorable_blacklist_item', return_value=True):
            self.assertTrue(is_fillable_by_pack(_row('Blacklisted', EXHAUSTED)))

    def test_manually_blacklisted_row_is_never_fillable(self):
        with patch('database.blacklist.is_restorable_blacklist_item', return_value=False):
            self.assertFalse(is_fillable_by_pack(_row('Blacklisted', EXHAUSTED)))

    def test_blacklist_without_exhausted_stage_is_not_fillable(self):
        with patch('database.blacklist.is_restorable_blacklist_item', return_value=True):
            self.assertFalse(is_fillable_by_pack(_row('Blacklisted', SCRAPE)))
            self.assertFalse(is_fillable_by_pack(_row('Blacklisted', None)))
            self.assertFalse(is_fillable_by_pack(_row('Blacklisted', '{not json')))

    def test_failure_stage_reader(self):
        self.assertEqual(failure_stage(_row('Dormant', SCRAPE)), 'scrape')
        self.assertIsNone(failure_stage(_row('Dormant', None)))
        self.assertIsNone(failure_stage(_row('Dormant', '[1,2]')))


class CandidateQuery(unittest.TestCase):
    def test_candidates_come_from_the_database_across_ladder_states(self):
        rows = [
            _row('Wanted', id=2), _row('Sleeping', id=3), _row('Dormant', id=4),
            _row('Blacklisted', EXHAUSTED, id=5), _row('Blacklisted', None, id=6),
            _row('Wanted', id=1),   # the primary item itself
        ]
        with patch('database.database_reading.get_all_media_items', return_value=rows) as get, \
                patch('database.blacklist.is_restorable_blacklist_item', return_value=True):
            candidates = _related_fill_candidates(_row('Adding', id=1))
        self.assertEqual(sorted(c['id'] for c in candidates), [2, 3, 4, 5])
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs['imdb_id'], 'tt1')
        self.assertEqual(kwargs['media_type'], 'episode')
        self.assertEqual(set(kwargs['state']), set(RELATED_FILL_STATES))

    def test_movies_and_idless_items_have_no_candidates(self):
        with patch('database.database_reading.get_all_media_items') as get:
            self.assertEqual(_related_fill_candidates({'type': 'movie', 'imdb_id': 'tt1'}), [])
            self.assertEqual(_related_fill_candidates({'type': 'episode'}), [])
        get.assert_not_called()

    def test_database_error_fails_closed_to_no_candidates(self):
        with patch('database.database_reading.get_all_media_items', side_effect=RuntimeError('locked')):
            self.assertEqual(_related_fill_candidates(_row('Adding')), [])


class CheckingAcceptsExhaustedBlacklist(unittest.TestCase):
    def _move(self, record, restorable):
        qm = QueueManager.__new__(QueueManager)
        qm._move_item_to_queue = MagicMock(return_value=None)
        with patch('database.blacklist.is_restorable_blacklist_item', return_value=restorable):
            qm.move_to_checking(_row('Blacklisted', record), 'Blacklisted',
                                title='Pack', link='magnet:?xt=urn:btih:abc', filled_by_file='01.mkv')
        return qm._move_item_to_queue

    def test_exhausted_row_may_be_filled(self):
        self._move(EXHAUSTED, True).assert_called_once()

    def test_manual_blacklist_stays_blocked(self):
        self._move(EXHAUSTED, False).assert_not_called()
        self._move(SCRAPE, True).assert_not_called()


if __name__ == '__main__':
    unittest.main()
