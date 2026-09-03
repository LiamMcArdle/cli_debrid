import json
import unittest
from unittest.mock import Mock, patch

from database.database_writing import update_partial_scrape
from queues.scraping_queue import ScrapingQueue


class TestPartialScrapePersistence(unittest.TestCase):
    def test_records_sorted_unique_sources_and_retry_time(self):
        connection = Mock()
        with patch('database.database_writing.get_db_connection', return_value=connection):
            self.assertTrue(update_partial_scrape(
                42, {'Zilean_1', 'Torrentio_1', 'Zilean_1'}, retry_minutes=15))

        sql, params = connection.execute.call_args.args
        self.assertIn('partial_scrape_sources = ?', sql)
        self.assertEqual(json.loads(params[0]), ['Torrentio_1', 'Zilean_1'])
        self.assertEqual(params[-1], 42)
        self.assertGreater(params[1], params[2])
        connection.commit.assert_called_once()
        connection.close.assert_called_once()

    def test_every_scrape_caller_records_incomplete_sources(self):
        queue = ScrapingQueue()
        manager = Mock()
        manager.generate_identifier.return_value = 'Test Movie (2020)'
        item = {
            'id': 42, 'imdb_id': 'tt1234567', 'tmdb_id': '123',
            'title': 'Test Movie', 'year': 2020, 'type': 'movie',
            'version': 'default', 'genres': [],
        }
        with patch('database.get_media_item_by_id',
                   return_value={'fall_back_to_single_scraper': False},
                   create=True), \
                patch('queues.scraping_queue.scrape', return_value=([{'title': 'release'}], [])), \
                patch('scraper.scrape_status.get_unavailable',
                      return_value={'Torrentio_1'}), \
                patch('database.database_writing.update_partial_scrape') as update, \
                patch('queues.scraping_queue.get_setting', return_value=30):
            results, _ = queue.scrape_with_fallback(
                item, False, manager, skip_filter=True)

        self.assertEqual(results, [{'title': 'release'}])
        update.assert_called_once_with(
            42, {'Torrentio_1'}, retry_minutes=30)
        self.assertEqual(item['partial_scrape_sources'], '["Torrentio_1"]')

    def test_every_scrape_caller_clears_completed_search_marker(self):
        queue = ScrapingQueue()
        manager = Mock()
        manager.generate_identifier.return_value = 'Test Movie (2020)'
        item = {
            'id': 42, 'imdb_id': 'tt1234567', 'tmdb_id': '123',
            'title': 'Test Movie', 'year': 2020, 'type': 'movie',
            'version': 'default', 'genres': [],
            'partial_scrape_sources': '["Torrentio_1"]',
        }
        with patch('database.get_media_item_by_id',
                   return_value={'fall_back_to_single_scraper': False},
                   create=True), \
                patch('queues.scraping_queue.scrape', return_value=([], [])), \
                patch('scraper.scrape_status.get_unavailable', return_value=set()), \
                patch('database.database_writing.update_partial_scrape') as update, \
                patch('queues.scraping_queue.get_setting', return_value=30):
            results, _ = queue.scrape_with_fallback(
                item, False, manager, skip_filter=True)

        self.assertEqual(results, [])
        update.assert_called_once_with(42, set(), retry_minutes=30)
        self.assertIsNone(item['partial_scrape_sources'])

    def test_complete_search_with_no_marker_writes_nothing(self):
        # The common case: every source answered and the row carried no marker.
        # Writing NULL over NULL here was one UPDATE+commit per scrape.
        queue = ScrapingQueue()
        manager = Mock()
        manager.generate_identifier.return_value = 'Test Movie (2020)'
        item = {
            'id': 42, 'imdb_id': 'tt1234567', 'tmdb_id': '123',
            'title': 'Test Movie', 'year': 2020, 'type': 'movie',
            'version': 'default', 'genres': [],
        }
        with patch('database.get_media_item_by_id',
                   return_value={'fall_back_to_single_scraper': False},
                   create=True), \
                patch('queues.scraping_queue.scrape', return_value=([{'title': 'release'}], [])), \
                patch('scraper.scrape_status.get_unavailable', return_value=set()), \
                patch('database.database_writing.update_partial_scrape') as update, \
                patch('queues.scraping_queue.get_setting', return_value=30):
            queue.scrape_with_fallback(item, False, manager, skip_filter=True)

        update.assert_not_called()
        self.assertIsNone(item.get('partial_scrape_sources'))

    def test_completed_search_clears_marker(self):
        connection = Mock()
        with patch('database.database_writing.get_db_connection', return_value=connection):
            self.assertTrue(update_partial_scrape(42, set()))

        sql, params = connection.execute.call_args.args
        self.assertIn('partial_scrape_sources = NULL', sql)
        self.assertEqual(params[-1], 42)
        connection.commit.assert_called_once()
        connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
