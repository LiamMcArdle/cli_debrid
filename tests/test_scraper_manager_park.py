"""A parked scraper is skipped without being charged to the circuit breaker.

Before parks were understood by the manager, every skipped call still raised
ScraperUnavailable, which run_scraper counted as a timeout: one ten-title
scrape tripped the 3-in-120s circuit and bolted a 10-minute backoff onto every
park, during which no success could clear the escalation.
"""

import unittest
from unittest.mock import patch

from scraper import scraper_manager
from scraper.scraper_manager import ScraperManager
from scraper.park import PARKS
from scraper.scrape_status import ScraperParked


class ParkedScrapersDoNotTripTheCircuit(unittest.TestCase):
    def setUp(self):
        for park in PARKS.values():
            park.clear()
        self.addCleanup(lambda: [park.clear() for park in PARKS.values()])
        scraper_manager._scraper_circuit.clear()
        self.addCleanup(scraper_manager._scraper_circuit.clear)

    def manager(self, scraper_type, scraper):
        instance = f'{scraper_type}_1'
        config = {'Scrapers': {instance: {'enabled': True, 'type': scraper_type}}}
        manager = ScraperManager(config)
        manager.scrapers[scraper_type] = scraper
        manager.get_scraper_settings = lambda name: (
            config['Scrapers'].get(name)
            or (config['Scrapers'][instance] if name == scraper_type else {})
        )
        manager._log_scraper_report = lambda *args, **kwargs: None
        manager._log_detailed_results = lambda *args, **kwargs: None
        manager._enrich_results = lambda results, summary: results
        return manager, instance

    def run_manager(self, manager, unavailable, **kwargs):
        defaults = dict(
            imdb_id='tt1234567', title='Test', year=2020,
            content_type='episode', season=1, episode=1,
            unavailable_scope=unavailable,
        )
        defaults.update(kwargs)
        with patch('scraper.scraper_manager.get_setting', return_value=5):
            return manager.scrape_all(**defaults)

    def test_parked_type_is_skipped_before_submission(self):
        calls = []
        manager, instance = self.manager('Torrentio', lambda **kw: calls.append(1) or [{'title': 'x'}])
        PARKS['Torrentio'].trip()
        unavailable = set()

        for _ in range(5):
            self.assertEqual(self.run_manager(manager, unavailable), [])

        self.assertEqual(calls, [])
        self.assertEqual(unavailable, {instance})
        self.assertNotIn(instance, scraper_manager._scraper_circuit)

    def test_scraper_parked_raised_by_worker_is_not_a_timeout(self):
        def parked(**kwargs):
            raise ScraperParked('parked', retry_after=30)
        manager, instance = self.manager('Torrentio', parked)
        unavailable = set()

        for _ in range(5):
            self.assertEqual(self.run_manager(manager, unavailable), [])

        self.assertEqual(unavailable, {instance})
        self.assertNotIn(instance, scraper_manager._scraper_circuit)

    def test_parked_nyaa_is_skipped_in_the_anime_prepass(self):
        calls = []
        manager, _ = self.manager('Nyaa', lambda **kw: calls.append(1) or [])
        PARKS['Nyaa'].trip()
        unavailable = set()

        self.assertEqual(self.run_manager(
            manager, unavailable, is_anime=True,
            episode_formats={'regular': 'S01E01'}), [])
        self.assertEqual(calls, [])
        self.assertIn('Nyaa', unavailable)
        self.assertNotIn('Nyaa', scraper_manager._scraper_circuit)


if __name__ == '__main__':
    unittest.main()
