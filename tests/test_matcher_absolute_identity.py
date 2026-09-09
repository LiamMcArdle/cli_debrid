"""The media matcher looks for the same absolute number the scraper searched for.

One Piece S21E77: the scraper (identity resolver) found and chose
'[SubsPlease] One Piece - 968'. The matcher then computed 77 -- its
"high season and episode means the episode number is already absolute"
heuristic -- and could not match the file it had just been handed, so the
item bounced Adding -> Wanted 46 times in a day. The matcher now resolves
the number the same way the scraper does, with the arithmetic and the
heuristic only as fallbacks, and asks for both numbers of a dual-tree episode.
"""
import unittest
from unittest.mock import patch

import database  # noqa: F401  (circular import: must be imported first)
from queues.media_matcher import MediaMatcher

ONE_PIECE = {'imdb_id': 'tt0388629', 'tmdb_id': '37854', 'title': 'One Piece', 'series_title': 'One Piece',
             'season_number': 21, 'episode_number': 77, 'episode_title': 'The King of Pirates Is Born!'}


class AbsoluteCandidates(unittest.TestCase):
    def setUp(self):
        self.matcher = MediaMatcher()

    def test_identity_resolver_wins_over_arithmetic_and_heuristic(self):
        with patch('scraper.scraper._resolve_battery_absolute', return_value=(968, None)) as resolve, \
                patch.object(self.matcher, '_get_season_episode_counts_cached', return_value={1: 61, 20: 100}):
            self.assertEqual(self.matcher._absolute_candidates_for_item(ONE_PIECE), (968,))
            self.assertEqual(self.matcher._compute_absolute_episode_for_item(ONE_PIECE), 968)
        resolve.assert_called_once()
        self.assertEqual(resolve.call_args.args[:3], ('tt0388629', 21, 77))

    def test_dual_tree_episode_yields_both_numbers(self):
        with patch('scraper.scraper._resolve_battery_absolute', return_value=(162, 1061)), \
                patch.object(self.matcher, '_get_season_episode_counts_cached', return_value={}):
            self.assertEqual(self.matcher._absolute_candidates_for_item(ONE_PIECE), (162, 1061))

    def test_arithmetic_when_the_battery_has_nothing(self):
        with patch('scraper.scraper._resolve_battery_absolute', return_value=(None, None)), \
                patch.object(self.matcher, '_get_season_episode_counts_cached', return_value={0: 5, 1: 26, 2: 24}):
            item = {**ONE_PIECE, 'season_number': 3, 'episode_number': 4}
            self.assertEqual(self.matcher._absolute_candidates_for_item(item), (54,))

    def test_heuristic_is_the_last_resort(self):
        with patch('scraper.scraper._resolve_battery_absolute', return_value=(None, None)), \
                patch.object(self.matcher, '_get_season_episode_counts_cached', return_value=None):
            self.assertEqual(self.matcher._absolute_candidates_for_item(ONE_PIECE), (77,))

    def test_result_is_cached_per_coordinate(self):
        with patch('scraper.scraper._resolve_battery_absolute', return_value=(968, None)) as resolve, \
                patch.object(self.matcher, '_get_season_episode_counts_cached', return_value={}):
            self.matcher._absolute_candidates_for_item(ONE_PIECE)
            self.matcher._absolute_candidates_for_item(dict(ONE_PIECE))
        resolve.assert_called_once()

    def test_missing_coordinates(self):
        self.assertEqual(self.matcher._absolute_candidates_for_item({'imdb_id': 'tt1'}), ())
        self.assertIsNone(self.matcher._compute_absolute_episode_for_item({'imdb_id': 'tt1'}))


if __name__ == '__main__':
    unittest.main()
