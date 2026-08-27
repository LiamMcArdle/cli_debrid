import unittest
from unittest.mock import patch

from scraper.functions.rank_results import rank_result_key


class TestRankingAccessibility(unittest.TestCase):
    def result(self, suffix, filename):
        title = f'Test Movie 2020 1080p WEB-DL {suffix}'.strip()
        return {
            'title': title,
            'original_title': title,
            'size': 4.0,
            'additional_metadata': {'filename': filename, 'bingeGroup': None},
            'parsed_info': {
                'title': 'Test Movie',
                'year': 2020,
                'resolution': '1080p',
                'resolution_rank': 3,
                'is_hdr': False,
                'languages': [],
            },
        }

    def score(self, result):
        settings = {
            'resolution_weight': 0,
            'hdr_weight': 0,
            'similarity_weight': 0,
            'size_weight': 0,
            'bitrate_weight': 0,
            'country_weight': 0,
            'language_weight': 1,
            'year_match_weight': 0,
            'max_resolution': '1080p',
        }
        with patch('utilities.settings.get_setting', return_value=False):
            rank_result_key(
                result, [result], 'Test Movie', 2020, None, None, False,
                'movie', settings,
            )
        return result['score_breakdown']['total_score']

    def test_explicit_english_subtitles_beat_unknown(self):
        english = self.result('ENG SUB', 'Test Movie English Subs.mkv')
        unknown = self.result('', 'Test Movie.mkv')
        self.assertGreater(self.score(english), self.score(unknown))

    def test_unknown_is_not_rewarded(self):
        first = self.result('', 'Test Movie.mkv')
        second = self.result('', 'Test Movie Alternate.mkv')
        self.assertEqual(self.score(first), self.score(second))

    def test_explicit_non_english_only_release_is_penalized(self):
        non_english = self.result('VOSTFR', 'Test Movie VOSTFR.mkv')
        unknown = self.result('', 'Test Movie.mkv')
        self.assertLess(self.score(non_english), self.score(unknown))


if __name__ == '__main__':
    unittest.main()
