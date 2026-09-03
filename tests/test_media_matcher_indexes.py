"""Files PTT cannot number must still become match candidates.

PTT drops a bare anime number when the filename also carries a year, so
'Hunter x Hunter 1999 -81- ...' parsed to no episode, was never indexed, and
the item looped add/match-nothing/remove against the same torrent. The
delimited number and the filename fallback are indexed as candidacy only;
_check_match still decides.
"""

import unittest
from unittest.mock import patch

# database must finish initializing before queues.media_matcher is imported.
import database  # noqa: F401
from queues.media_matcher import MediaMatcher


def _pf(filename, episodes=None, seasons=None, fallback=None, date=None):
    parsed = {'original_filename': filename, 'episodes': episodes or [],
              'seasons': seasons or []}
    if fallback is not None:
        parsed['fallback_episode'] = fallback
    if date is not None:
        parsed['date'] = date
    return {'path': filename, 'parsed_info': parsed}


class CandidateIndexing(unittest.TestCase):
    def setUp(self):
        with patch('utilities.settings.get_setting', return_value=False):
            self.matcher = MediaMatcher()

    def test_delimited_anime_number_is_indexed_not_the_year(self):
        pf = _pf('[Samir755] Hunter X Hunter 1999 -81- Ghost Sighting.mkv')
        idx = self.matcher._build_parsed_file_indexes([pf])
        self.assertIn(pf, idx['by_episode_only'][81])
        self.assertIn(pf, idx['by_season_episode'][(None, 81)])
        self.assertNotIn(1999, idx['by_episode_only'])

    def test_fallback_episode_is_indexed(self):
        pf = _pf('Show 2019 - 05.mkv', fallback=5)
        idx = self.matcher._build_parsed_file_indexes([pf])
        self.assertIn(pf, idx['by_episode_only'][5])

    def test_ptt_episodes_still_take_precedence(self):
        pf = _pf('Show - S02E03 -81-.mkv', episodes=[3], seasons=[2], fallback=81)
        idx = self.matcher._build_parsed_file_indexes([pf])
        self.assertIn(pf, idx['by_season_episode'][(2, 3)])
        self.assertNotIn(81, idx['by_episode_only'])

    def test_special_content_is_skipped(self):
        pf = _pf('Show NCOP -01-.mkv')
        pf['parsed_info']['is_anime_special_content'] = True
        idx = self.matcher._build_parsed_file_indexes([pf])
        self.assertEqual(dict(idx['by_episode_only']), {})


class FilenameFallback(unittest.TestCase):
    def setUp(self):
        with patch('utilities.settings.get_setting', return_value=False):
            self.matcher = MediaMatcher()

    def test_year_is_not_an_episode(self):
        self.assertEqual(self.matcher._extract_episode_from_filename('Show 2019 - 05.mkv'), 5)
        self.assertEqual(self.matcher._extract_episode_from_filename('Hunter x Hunter 1999 - 81.mkv'), 81)

    def test_resolution_suffix_is_skipped(self):
        self.assertEqual(self.matcher._extract_episode_from_filename('Show.1080p.03.mkv'), 3)
        self.assertEqual(self.matcher._extract_episode_from_filename('Show 10bit 04.mkv'), 4)

    def test_season_prefix_is_skipped(self):
        self.assertEqual(self.matcher._extract_episode_from_filename('Show S02 - 07.mkv'), 7)

    def test_explicit_markers_win(self):
        self.assertEqual(self.matcher._extract_episode_from_filename('Show 2020 E12.mkv'), 12)
        self.assertEqual(self.matcher._extract_episode_from_filename('Show episode 9.mkv'), 9)

    def test_nothing_usable(self):
        self.assertIsNone(self.matcher._extract_episode_from_filename('Show 2019 1080p.mkv'))
        self.assertIsNone(self.matcher._extract_episode_from_filename('Show.NCOP.01.mkv'))


class TitleGateIsReadOnce(unittest.TestCase):
    def test_setting_read_at_construction(self):
        with patch('utilities.settings.get_setting',
                   side_effect=lambda s, k, d=None: True if k == 'enforce_file_title_match' else d):
            self.assertTrue(MediaMatcher()._enforce_file_title)
        with patch('utilities.settings.get_setting', return_value=False):
            self.assertFalse(MediaMatcher()._enforce_file_title)


if __name__ == '__main__':
    unittest.main()
