"""Process-wide scraper parks: escalation, expiry, and what a park costs.

The park exists so a host-wide rate limit is paid once rather than once per
item. Two properties matter and are pinned here: the escalation only deepens
when we trip again inside the previous cooldown (otherwise every unrelated 429
hours apart would compound toward the cap), and a skipped call is reported as
ScraperParked so the manager can tell our own decision apart from an observed
failure.
"""

import unittest
from unittest.mock import patch

from scraper.park import (ScraperPark, PARKS, park_remaining,
                          longest_park_remaining)
from scraper.scrape_status import ScraperUnavailable, ScraperParked


class ScraperParkEscalation(unittest.TestCase):
    def setUp(self):
        self.park = ScraperPark('Test', base_seconds=100, max_seconds=400)

    def test_first_trip_uses_the_base_cooldown(self):
        with patch('scraper.park.time.time', return_value=1000.0):
            self.assertEqual(self.park.trip(), 100)
            self.assertEqual(self.park.remaining(), 100)

    def test_trip_while_parked_does_not_extend_or_escalate(self):
        with patch('scraper.park.time.time', return_value=1000.0):
            self.park.trip()
        with patch('scraper.park.time.time', return_value=1050.0):
            # A second thread racing into the same block must not push the
            # deadline out; it just learns how long is left.
            self.assertEqual(self.park.trip(), 50)
            self.assertEqual(self.park.remaining(), 50)

    def test_retrip_inside_the_previous_cooldown_doubles(self):
        with patch('scraper.park.time.time', return_value=1000.0):
            self.park.trip()            # parked until 1100
        with patch('scraper.park.time.time', return_value=1150.0):
            self.assertEqual(self.park.trip(), 200)

    def test_retrip_long_after_expiry_resets_to_base(self):
        with patch('scraper.park.time.time', return_value=1000.0):
            self.park.trip()            # parked until 1100, cooldown 100
        with patch('scraper.park.time.time', return_value=5000.0):
            self.assertEqual(self.park.trip(), 100)

    def test_escalation_is_capped(self):
        now = 1000.0
        for _ in range(6):
            with patch('scraper.park.time.time', return_value=now):
                length = self.park.trip()
            now += length + 1     # re-trip just after expiry, inside the window
        self.assertEqual(length, 400)

    def test_clear_resets_deadline_and_escalation(self):
        with patch('scraper.park.time.time', return_value=1000.0):
            self.park.trip()
            self.park.trip()
            self.park.clear()
            self.assertEqual(self.park.remaining(), 0)
            self.assertEqual(self.park.trip(), 100)

    def test_expired_park_reports_no_remaining_time(self):
        with patch('scraper.park.time.time', return_value=1000.0):
            self.park.trip()
        with patch('scraper.park.time.time', return_value=1101.0):
            self.assertEqual(self.park.remaining(), 0)


class RegistryLookups(unittest.TestCase):
    def setUp(self):
        for park in PARKS.values():
            park.clear()
        self.addCleanup(lambda: [park.clear() for park in PARKS.values()])

    def test_unparked_scrapers_report_zero(self):
        self.assertEqual(park_remaining('Nyaa'), 0)
        self.assertEqual(longest_park_remaining(), (None, 0.0))

    def test_unknown_scraper_is_never_parked(self):
        self.assertEqual(park_remaining('Prowlarr'), 0)

    def test_longest_park_wins(self):
        PARKS['Torrentio'].trip()          # 120s base
        PARKS['Nyaa'].trip()               # 900s base
        name, seconds = longest_park_remaining()
        self.assertEqual(name, 'Nyaa')
        self.assertGreater(seconds, 800)


class ParkedIsADistinctUnavailable(unittest.TestCase):
    """The queue catches ScraperUnavailable broadly; the manager needs the
    narrower type to decide whether to charge the circuit breaker."""

    def test_parked_is_a_scraper_unavailable(self):
        self.assertTrue(issubclass(ScraperParked, ScraperUnavailable))

    def test_retry_after_is_carried(self):
        error = ScraperParked('parked', retry_after=42.0)
        self.assertEqual(error.retry_after, 42.0)

    def test_retry_after_defaults_to_zero(self):
        self.assertEqual(ScraperParked('parked').retry_after, 0.0)


if __name__ == '__main__':
    unittest.main()
