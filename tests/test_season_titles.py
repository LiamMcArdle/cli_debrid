"""Season titles reach the identity gate, and a stale absolute ceiling is withheld.

The battery had no per-season names at all, so an arc-named sequel season
('Thousand-Year Blood War - 01') could never be tied to S17. The ceiling on
absolute numbers rejected every new batch of an ongoing show whenever the
battery's copy of it was stale.
"""

import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import database  # noqa: F401  (import order: database before scraper/)
from cli_battery.app import direct_api
from cli_battery.app.direct_api import season_titles_from, DirectAPI, SEASON_TITLES_KEY
from scraper import scraper as scraper_mod


class SeasonTitlesFromProviderData(unittest.TestCase):
    def test_real_titles_are_kept_generic_ones_dropped(self):
        seasons = {
            0: {'title': 'Specials', 'episodes': {}},
            1: {'title': 'Season 1', 'episodes': {}},
            16: {'title': None, 'episodes': {}},
            17: {'title': 'Thousand-Year Blood War', 'episodes': {}},
            '18': {'title': 'Thousand-Year Blood War - The Separation', 'episodes': {}},
            19: {'title': 'Part 3'},
            20: 'not a dict',
        }
        self.assertEqual(season_titles_from(seasons), {
            '17': ['Thousand-Year Blood War'],
            '18': ['Thousand-Year Blood War - The Separation'],
        })

    def test_empty_input(self):
        self.assertEqual(season_titles_from({}), {})
        self.assertEqual(season_titles_from(None), {})


class GetShowSeasonTitles(unittest.TestCase):
    def _session_with(self, item):
        session = MagicMock()
        session.query.return_value.options.return_value.filter_by.return_value.first.return_value = item

        @contextmanager
        def fake():
            yield session
        return fake

    def test_reads_and_normalises_keys(self):
        md = SimpleNamespace(key=SEASON_TITLES_KEY, value=json.dumps({'17': ['TYBW'], 'x': ['bad'], '18': []}))
        item = SimpleNamespace(item_metadata=[SimpleNamespace(key='aliases', value='{}'), md])
        with patch.object(direct_api, 'managed_session', self._session_with(item)):
            self.assertEqual(DirectAPI.get_show_season_titles('tt1'), {17: ['TYBW']})

    def test_double_encoded_rows_are_tolerated(self):
        md = SimpleNamespace(key=SEASON_TITLES_KEY, value=json.dumps(json.dumps({'2': ['Arc']})))
        item = SimpleNamespace(item_metadata=[md])
        with patch.object(direct_api, 'managed_session', self._session_with(item)):
            self.assertEqual(DirectAPI.get_show_season_titles('tt1'), {2: ['Arc']})

    def test_unknown_show_or_no_row_is_empty(self):
        with patch.object(direct_api, 'managed_session', self._session_with(None)):
            self.assertEqual(DirectAPI.get_show_season_titles('tt1'), {})
        item = SimpleNamespace(item_metadata=[])
        with patch.object(direct_api, 'managed_session', self._session_with(item)):
            self.assertEqual(DirectAPI.get_show_season_titles('tt1'), {})
        self.assertEqual(DirectAPI.get_show_season_titles(None), {})


class StaleAbsoluteCeiling(unittest.TestCase):
    def _run(self, status, fetched_ago, maximum=148):
        item = SimpleNamespace(id=1, media_status=status,
                               last_trakt_fetch=datetime.now(timezone.utc) - fetched_ago)
        session = MagicMock()

        def query(*args):
            q = MagicMock()
            q.filter_by.return_value.first.return_value = item
            q.join.return_value.filter.return_value.scalar.return_value = maximum
            return q
        session.query.side_effect = query
        session_cm = MagicMock()
        session_cm.__enter__.return_value = session
        session_cm.__exit__.return_value = False
        with patch('cli_battery.app.database.Session', return_value=session_cm):
            return scraper_mod.get_max_absolute_episode_from_database('tt1')

    def test_fresh_returning_show_returns_ceiling(self):
        self.assertEqual(self._run('returning series', timedelta(hours=1)), 148)

    def test_stale_returning_show_withholds_ceiling(self):
        self.assertIsNone(self._run('returning series', timedelta(days=2)))

    def test_ended_show_ceiling_is_final_even_when_stale(self):
        self.assertEqual(self._run('ended', timedelta(days=400)), 148)

    def test_unknown_show(self):
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        session_cm = MagicMock()
        session_cm.__enter__.return_value = session
        session_cm.__exit__.return_value = False
        with patch('cli_battery.app.database.Session', return_value=session_cm):
            self.assertIsNone(scraper_mod.get_max_absolute_episode_from_database('tt1'))


if __name__ == '__main__':
    unittest.main()
