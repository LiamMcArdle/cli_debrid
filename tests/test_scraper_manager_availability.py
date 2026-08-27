import time
import unittest
from unittest.mock import patch

from scraper.scraper_manager import ScraperManager


class TestScraperManagerAvailability(unittest.TestCase):
    def manager(self, scraper_type, scraper):
        instance = f'{scraper_type}_1'
        config = {
            'Scrapers': {
                instance: {'enabled': True, 'type': scraper_type},
            }
        }
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

    @staticmethod
    def slow_scraper(**kwargs):
        time.sleep(0.05)
        return []

    def run_manager(self, manager, unavailable, **kwargs):
        defaults = dict(
            imdb_id='tt1234567', title='Test', year=2020,
            content_type='episode', season=1, episode=1,
            unavailable_scope=unavailable,
        )
        defaults.update(kwargs)
        with patch('scraper.scraper_manager.get_setting', return_value=0.005):
            return manager.scrape_all(**defaults)

    def test_batch_timeout_is_recorded_as_unavailable(self):
        manager, instance = self.manager('Torrentio', self.slow_scraper)
        unavailable = set()

        self.assertEqual(self.run_manager(manager, unavailable), [])
        self.assertEqual(unavailable, {instance})

    def test_circuit_open_skip_is_recorded_as_unavailable(self):
        manager, instance = self.manager('Torrentio', lambda **kwargs: [])
        unavailable = set()

        with patch('scraper.scraper_manager._circuit_is_open',
                   side_effect=lambda name: name == instance):
            self.assertEqual(self.run_manager(manager, unavailable), [])
        self.assertEqual(unavailable, {instance})

    def test_anime_prepass_timeout_is_recorded_as_unavailable(self):
        manager, configured_instance = self.manager('Nyaa', self.slow_scraper)
        unavailable = set()

        self.assertEqual(self.run_manager(
            manager, unavailable, is_anime=True,
            episode_formats={'regular': 'S01E01'}), [])
        # The anime pre-pass intentionally uses the canonical Nyaa instance.
        self.assertEqual(unavailable, {'Nyaa'})
        self.assertNotIn(configured_instance, unavailable)


if __name__ == '__main__':
    unittest.main()
