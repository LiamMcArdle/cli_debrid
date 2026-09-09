"""What the Prowlarr fetch path remembers between two identical queries.

Measured 2026-09-09: 75-80% of Prowlarr's queries return nothing, and the
cache never stored an empty list, so the emptiest queries were the ones
re-sent on every fallback re-entry and every sibling. An outage is also
remembered -- as a raise, never as an empty list, because the retry ladder
now holds a rung for a scrape nobody answered and an empty answer would spend
it. Prowlarr's own disabled indexers are logged against an empty answer and
never turned into an outage: with AnimeTosho disabled dozens of times a day
that would hold every zero-result scrape.
"""
import unittest
from unittest.mock import MagicMock, patch

import requests

import database  # noqa: F401  (circular import: must be imported first)
from scraper import prowlarr
from scraper.scrape_status import ScraperUnavailable

SETTINGS = {'url': 'http://prowlarr:9696', 'api': 'key'}


def _response(status, payload):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.text = ''
    return response


def _scrape():
    return prowlarr.scrape_prowlarr_instance(
        'P', SETTINGS, 'tt0388629', 'One Piece', 1999, 'episode', season=19, episode=10)


class FetchCache(unittest.TestCase):
    def setUp(self):
        prowlarr._NZB_CACHE.clear()
        prowlarr._INDEXER_STATUS_CACHE.clear()
        self.addCleanup(prowlarr._NZB_CACHE.clear)
        self.addCleanup(prowlarr._INDEXER_STATUS_CACHE.clear)

    def _status_calls(self, get):
        return [c for c in get.call_args_list if 'indexerstatus' in c.args[0]]

    def _search_calls(self, get):
        return [c for c in get.call_args_list if c.args[0].endswith('/api/v1/search')]

    def test_an_empty_answer_is_served_from_cache_the_second_time(self):
        with patch.object(prowlarr.api, 'get', return_value=_response(200, [])) as get:
            self.assertEqual(_scrape(), [])
            first = len(self._search_calls(get))
            self.assertEqual(_scrape(), [])
            self.assertEqual(len(self._search_calls(get)), first)
        self.assertGreater(first, 0)

    def test_an_outage_is_remembered_as_a_raise(self):
        def unreachable(url, **kwargs):
            if url.endswith('/api/v1/search'):
                raise requests.exceptions.ConnectionError('refused')
            return _response(200, [])
        with patch.object(prowlarr.api, 'get', side_effect=unreachable) as get:
            with self.assertRaises(ScraperUnavailable):
                _scrape()
            first = len(self._search_calls(get))
            with self.assertRaises(ScraperUnavailable) as raised:
                _scrape()
            self.assertEqual(len(self._search_calls(get)), first)
        self.assertIn('(cached)', str(raised.exception))

    def test_a_cached_outage_expires_before_a_cached_empty(self):
        prowlarr._cache_set('outage', prowlarr._Unavailable('down'), ttl=prowlarr._NZB_CACHE_UNAVAILABLE_TTL)
        prowlarr._cache_set('empty', [])
        later = prowlarr.time.monotonic() + prowlarr._NZB_CACHE_UNAVAILABLE_TTL + 1
        with patch.object(prowlarr.time, 'monotonic', return_value=later):
            self.assertIsNone(prowlarr._cache_get('outage'))
            self.assertEqual(prowlarr._cache_get('empty'), [])

    def test_every_request_asks_for_two_hundred_rows(self):
        with patch.object(prowlarr.api, 'get', return_value=_response(200, [])) as get:
            _scrape()
        for call in self._search_calls(get):
            self.assertEqual(call.kwargs['params']['limit'], 200)


class DisabledIndexersAreLoggedNotHeld(unittest.TestCase):
    STATUS = [{'indexerId': 4, 'disabledTill': '2026-09-09T13:15:29Z',
               'mostRecentFailure': '2026-09-09T12:15:29Z'}]
    INDEXERS = [{'id': 4, 'name': 'AnimeTosho'}, {'id': 1, 'name': 'Zilean'}]

    def setUp(self):
        prowlarr._NZB_CACHE.clear()
        prowlarr._INDEXER_STATUS_CACHE.clear()
        self.addCleanup(prowlarr._NZB_CACHE.clear)
        self.addCleanup(prowlarr._INDEXER_STATUS_CACHE.clear)

    def _get(self, search_payload, status_payload=None, status_code=200):
        def get(url, **kwargs):
            if url.endswith('/api/v1/indexerstatus'):
                return _response(status_code, status_payload if status_payload is not None else self.STATUS)
            if url.endswith('/api/v1/indexer'):
                return _response(200, self.INDEXERS)
            return _response(200, search_payload)
        return get

    def test_empty_answer_names_the_disabled_indexer_and_returns_empty(self):
        with patch.object(prowlarr.api, 'get', side_effect=self._get([])), \
                self.assertLogs(level='INFO') as logs:
            self.assertEqual(_scrape(), [])
        self.assertTrue(any('disabled inside Prowlarr: AnimeTosho (until 2026-09-09 13:15Z)' in line
                            for line in logs.output), logs.output)

    def test_results_are_returned_without_consulting_status(self):
        payload = [{'title': 'One Piece 1069', 'guid': 'g1', 'size': 1, 'seeders': 3,
                    'downloadUrl': 'magnet:?xt=urn:btih:' + 'a' * 40, 'indexer': 'Zilean'}]
        with patch.object(prowlarr.api, 'get', side_effect=self._get(payload)) as get:
            results = _scrape()
        self.assertTrue(results)
        self.assertFalse([c for c in get.call_args_list if 'indexerstatus' in c.args[0]])

    def test_status_failure_is_silent(self):
        with patch.object(prowlarr.api, 'get', side_effect=self._get([], status_code=500)), \
                self.assertLogs(level='INFO') as logs:
            self.assertEqual(_scrape(), [])
        self.assertFalse(any('disabled inside Prowlarr' in line for line in logs.output))

    def test_status_is_cached_for_a_minute(self):
        with patch.object(prowlarr.api, 'get', side_effect=self._get([])) as get:
            _scrape()
            prowlarr._NZB_CACHE.clear()
            _scrape()
        self.assertEqual(len([c for c in get.call_args_list if 'indexerstatus' in c.args[0]]), 1)


if __name__ == '__main__':
    unittest.main()
