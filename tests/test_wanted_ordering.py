"""The Wanted candidate order must not let wakes cut in front of the backlog.

Upstream ordered candidates by title alone. Every wake from Sleeping/Dormant
re-enters Wanted, so any A-M show that kept failing sorted ahead of an N-Z
item that had waited for days; One Piece (794 rows) went untouched for five
days while BLEACH cycled. These tests pin the replacement order against a real
SQLite table, because the CASE/GLOB/strftime expressions are the whole point.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

# database must finish initializing before queues.wanted_queue is imported.
import database  # noqa: F401
from queues.wanted_queue import wanted_order_by


class WantedOrdering(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('''CREATE TABLE media_items (
            id INTEGER PRIMARY KEY, title TEXT, type TEXT, state TEXT,
            season_number INTEGER, episode_number INTEGER,
            release_date TEXT, last_updated TIMESTAMP, ghostlisted INTEGER)''')

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def _add(self, title, release_date, touched_ago, season=1, episode=1, type_='episode'):
        touched = datetime.now() - touched_ago
        self.conn.execute(
            "INSERT INTO media_items (title, type, state, season_number, episode_number, "
            "release_date, last_updated) VALUES (?, ?, 'Wanted', ?, ?, ?, ?)",
            (title, type_, season, episode, release_date, touched))

    def _order(self, sort_order='None', by_release=False, recent_days=14):
        clauses, params = wanted_order_by(sort_order, by_release, recent_days)
        rows = self.conn.execute(
            "SELECT title, season_number, episode_number FROM media_items "
            "WHERE state = 'Wanted' ORDER BY " + ", ".join(clauses), params).fetchall()
        return [(r['title'], r['season_number'], r['episode_number']) for r in rows]

    def test_oldest_touched_beats_alphabet(self):
        old = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
        self._add('BLEACH', old, timedelta(minutes=5))          # just woke
        self._add('One Piece', old, timedelta(days=5))          # waited five days
        self._add('Zeta', old, timedelta(days=2))
        self.assertEqual([t for t, _, _ in self._order()],
                         ['One Piece', 'Zeta', 'BLEACH'])

    def test_recent_release_goes_first_even_if_just_touched(self):
        old = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
        fresh = datetime.now().strftime('%Y-%m-%d')
        self._add('One Piece', old, timedelta(days=5))
        self._add('Zeta', fresh, timedelta(minutes=1))          # aired today
        self.assertEqual([t for t, _, _ in self._order()], ['Zeta', 'One Piece'])

    def test_unknown_release_date_is_not_treated_as_recent(self):
        old = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
        self._add('One Piece', old, timedelta(days=5))
        self._add('Mystery', 'Unknown', timedelta(minutes=1))
        self._add('Blank', None, timedelta(minutes=1))
        self.assertEqual([t for t, _, _ in self._order()],
                         ['One Piece', 'Blank', 'Mystery'])

    def test_recent_window_is_configurable(self):
        twenty_days = (datetime.now() - timedelta(days=20)).strftime('%Y-%m-%d')
        old = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
        self._add('One Piece', old, timedelta(days=5))
        self._add('Zeta', twenty_days, timedelta(minutes=1))
        self.assertEqual([t for t, _, _ in self._order(recent_days=14)][0], 'One Piece')
        self.assertEqual([t for t, _, _ in self._order(recent_days=30)][0], 'Zeta')

    def test_a_shows_episodes_stay_contiguous_within_the_hour(self):
        old = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
        base = timedelta(days=3)
        self._add('Show', old, base + timedelta(minutes=3), season=1, episode=2)
        self._add('Aardvark', old, base + timedelta(minutes=2))
        self._add('Show', old, base + timedelta(minutes=1), season=1, episode=1)
        self.assertEqual(self._order(),
                         [('Aardvark', 1, 1), ('Show', 1, 1), ('Show', 1, 2)])

    def test_release_date_sort_setting_still_wins(self):
        old = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
        older = (datetime.now() - timedelta(days=800)).strftime('%Y-%m-%d')
        self._add('One Piece', older, timedelta(days=5))
        self._add('Zeta', old, timedelta(minutes=1))
        clauses, params = wanted_order_by('None', True, 14)
        self.assertEqual(params, [])
        self.assertEqual([t for t, _, _ in self._order(by_release=True)], ['Zeta', 'One Piece'])

    def test_movies_first_still_leads(self):
        old = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
        self._add('Aardvark', old, timedelta(days=5))
        self._add('Zeta Film', old, timedelta(minutes=1), type_='movie')
        self.assertEqual([t for t, _, _ in self._order(sort_order='Movies First')][0], 'Zeta Film')


if __name__ == '__main__':
    unittest.main()
