import importlib.util
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scraper.scrape_status import ScraperUnavailable


class _RequestException(Exception):
    pass


_fake_api = SimpleNamespace(
    get=Mock(), exceptions=SimpleNamespace(RequestException=_RequestException))
_routes = types.ModuleType('routes')
_api_tracker = types.ModuleType('routes.api_tracker')
_api_tracker.api = _fake_api
_database = types.ModuleType('database')
_database_reading = types.ModuleType('database.database_reading')
_database_reading.get_imdb_aliases = lambda imdb_id: [imdb_id]

with patch.dict(sys.modules, {
    'routes': _routes,
    'routes.api_tracker': _api_tracker,
    'database': _database,
    'database.database_reading': _database_reading,
}):
    _path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         'scraper', 'torrentio.py')
    _spec = importlib.util.spec_from_file_location('torrentio_under_test', _path)
    torrentio = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(torrentio)


class TestTorrentioAvailability(unittest.TestCase):
    def setUp(self):
        # A 429 parks Torrentio process-wide; tests must not inherit each
        # other's park.
        torrentio.PARKS['Torrentio'].clear()
        self.addCleanup(torrentio.PARKS['Torrentio'].clear)

    def response(self, status, payload=None):
        return SimpleNamespace(status_code=status, json=lambda: payload or {})

    def test_server_errors_retry_then_report_unavailable(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status), \
                    patch.object(torrentio.api, 'get', return_value=self.response(status)) as get, \
                    patch.object(torrentio.time, 'sleep') as sleep, \
                    patch.object(torrentio.random, 'uniform', return_value=0):
                with self.assertRaises(ScraperUnavailable):
                    torrentio.fetch_data('https://example.invalid/stream.json')
                self.assertEqual(get.call_count, 4)
                self.assertEqual(sleep.call_count, 3)

    def test_rate_limit_is_immediately_unavailable(self):
        with patch.object(torrentio.api, 'get', return_value=self.response(429)) as get, \
                patch.object(torrentio.time, 'sleep') as sleep:
            with self.assertRaises(ScraperUnavailable):
                torrentio.fetch_data('https://example.invalid/stream.json')
        get.assert_called_once()
        sleep.assert_not_called()
        # The 429 parks Torrentio for the whole process: the next call is
        # skipped without a round trip and reported as a park, not a failure.
        self.assertGreater(torrentio.PARKS['Torrentio'].remaining(), 0)
        with patch.object(torrentio.api, 'get', return_value=self.response(200, {})) as get:
            with self.assertRaises(torrentio.ScraperParked):
                torrentio.fetch_data('https://example.invalid/stream.json')
        get.assert_not_called()

    def test_success_clears_the_park(self):
        torrentio.PARKS['Torrentio'].trip()
        torrentio.PARKS['Torrentio']._blocked_until = 0.0   # park expired
        with patch.object(torrentio.api, 'get', return_value=self.response(200, {'streams': []})):
            torrentio.fetch_data('https://example.invalid/stream.json')
        self.assertEqual(torrentio.PARKS['Torrentio']._cooldown,
                         torrentio.PARKS['Torrentio'].base_seconds)

    def test_non_retryable_response_is_a_completed_empty_search(self):
        with patch.object(torrentio.api, 'get', return_value=self.response(404)) as get:
            self.assertEqual(
                torrentio.fetch_data('https://example.invalid/stream.json'), {})
        get.assert_called_once()

    def test_success_returns_payload(self):
        payload = {'streams': [{'title': 'release'}]}
        with patch.object(torrentio.api, 'get', return_value=self.response(200, payload)):
            self.assertEqual(
                torrentio.fetch_data('https://example.invalid/stream.json'), payload)


if __name__ == '__main__':
    unittest.main()
