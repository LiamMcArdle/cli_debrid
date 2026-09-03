"""Nyaa's 429 handling must key on the HTTP status, not the error text.

raise_for_status() embeds the request URL in the exception message, so a
substring test for '429' parked Nyaa process-wide whenever the QUERY contained
that number -- One Piece episode 429 timing out was enough. These tests pin the
status-code contract and the ScraperParked skip.
"""

import unittest
from unittest.mock import MagicMock, patch
from contextlib import contextmanager

import requests

# database must finish initializing before anything under scraper/ is
# imported: scraper.functions -> database -> routes -> scraper.scraper ->
# scraper_manager -> nyaa re-enters the package and dies on a circular import.
import database  # noqa: F401
from scraper import nyaa
from scraper.park import PARKS
from scraper.scrape_status import ScraperUnavailable, ScraperParked


def _http_error(status_code: int, url: str = 'https://nyaa.si/?q=x') -> requests.HTTPError:
    response = MagicMock()
    response.status_code = status_code
    response.url = url
    return requests.HTTPError(f"{status_code} Client Error for url: {url}", response=response)


@contextmanager
def _fake_session():
    yield MagicMock()


class Nyaa429Detection(unittest.TestCase):
    def setUp(self):
        PARKS['Nyaa'].clear()
        self.addCleanup(PARKS['Nyaa'].clear)
        self.sleep = patch('scraper.nyaa.time.sleep', return_value=None)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.session = patch('scraper.nyaa._warp_proxy_context', _fake_session)
        self.session.start()
        self.addCleanup(self.session.stop)

    def _run(self, side_effect, max_retries=3):
        with patch('scraper.nyaa._search_nyaa_with_session', side_effect=side_effect) as search:
            try:
                return nyaa.scrape_nyaa_with_retry('One Piece 429', 1, 2, 0, max_retries=max_retries), search
            except Exception as e:
                return e, search

    def test_http_429_parks_nyaa_and_is_unavailable(self):
        result, search = self._run(_http_error(429))
        self.assertIsInstance(result, ScraperUnavailable)
        self.assertNotIsInstance(result, ScraperParked)
        self.assertGreater(PARKS['Nyaa'].remaining(), 0)
        self.assertEqual(search.call_count, 1)

    def test_url_containing_429_does_not_park(self):
        # A non-HTTP failure whose message happens to mention 429 (the query
        # text) is neither a rate limit nor retryable.
        result, search = self._run(ValueError("bad payload for query 'One Piece 429'"))
        self.assertIsInstance(result, ValueError)
        self.assertEqual(PARKS['Nyaa'].remaining(), 0)
        self.assertEqual(search.call_count, 1)

    def test_http_503_is_retried_then_raised(self):
        result, search = self._run(_http_error(503), max_retries=3)
        self.assertIsInstance(result, requests.HTTPError)
        self.assertEqual(search.call_count, 3)
        self.assertEqual(PARKS['Nyaa'].remaining(), 0)

    def test_timeout_is_retried(self):
        result, search = self._run(requests.exceptions.Timeout('read timed out'), max_retries=2)
        self.assertIsInstance(result, requests.exceptions.Timeout)
        self.assertEqual(search.call_count, 2)

    def test_success_after_retry_clears_the_park(self):
        PARKS['Nyaa'].trip()
        # Simulate the park having expired so the call goes through.
        PARKS['Nyaa']._blocked_until = 0.0
        result, search = self._run([_http_error(502), ['torrent']])
        self.assertEqual(result, ['torrent'])
        self.assertEqual(PARKS['Nyaa'].remaining(), 0)
        self.assertEqual(PARKS['Nyaa']._cooldown, PARKS['Nyaa'].base_seconds)

    def test_parked_call_is_skipped_as_scraper_parked(self):
        PARKS['Nyaa'].trip()
        result, search = self._run(['torrent'])
        self.assertIsInstance(result, ScraperParked)
        self.assertGreater(result.retry_after, 0)
        self.assertEqual(search.call_count, 0)


class SingleFormatForAliasTitles(unittest.TestCase):
    """An alias title runs one Nyaa format; the main title keeps them all."""

    FORMATS = {'regular': 'S01E05', 'absolute': '017', 'no_zeros': '17'}

    def test_recorded_preference_wins(self):
        with patch('scraper.nyaa.get_anime_format', return_value='regular'):
            self.assertEqual(nyaa.pick_single_format(self.FORMATS, 'tmdb1'),
                             {'regular': 'S01E05'})

    def test_absolute_when_no_preference(self):
        with patch('scraper.nyaa.get_anime_format', return_value=None):
            self.assertEqual(nyaa.pick_single_format(self.FORMATS, 'tmdb1'),
                             {'absolute': '017'})

    def test_first_format_when_neither_available(self):
        with patch('scraper.nyaa.get_anime_format', return_value='combined'):
            self.assertEqual(nyaa.pick_single_format({'regular': 'S01E05', 'no_zeros': '17'}, 'tmdb1'),
                             {'regular': 'S01E05'})

    def _episode_scrape(self, single_format):
        with patch('scraper.nyaa._scrape_nyaa_with_format', return_value=[]) as fmt, \
                patch('scraper.nyaa.get_anime_format', return_value=None), \
                patch('scraper.nyaa.update_anime_format'), \
                patch('scraper.nyaa.time.sleep', return_value=None):
            nyaa.scrape_nyaa_anime_episode('Show', 2020, 1, 5, dict(self.FORMATS), 'tmdb1',
                                           single_format=single_format)
        return fmt.call_count

    def test_alias_title_makes_one_request(self):
        self.assertEqual(self._episode_scrape(single_format=True), 1)

    def test_main_title_tries_every_format(self):
        self.assertEqual(self._episode_scrape(single_format=False), len(self.FORMATS))


if __name__ == '__main__':
    unittest.main()
