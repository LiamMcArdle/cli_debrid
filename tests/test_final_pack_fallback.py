"""The final pack attempt keeps pack mode even when the single-scraper flag is set.

fall_back_to_single_scraper is swept onto every later episode of a season the
moment one pack add fails. It used to win inside scrape_with_fallback for
every call, including the caller's deliberate "final multi-pack fallback", so
one failed add put whole seasons into single-episode mode -- where every pack
is rejected on sight -- until a wake cleared it. Measured 2026-09-09: 1,286
pending rows, One Piece 280 of 282.
"""
import unittest
from unittest.mock import Mock, patch

import database  # noqa: F401  (circular import: must be imported first)
from queues.scraping_queue import ScrapingQueue

ITEM = {'id': 42, 'imdb_id': 'tt0388629', 'tmdb_id': '37854', 'title': 'One Piece', 'year': 1999,
        'type': 'episode', 'version': 'Anime', 'season_number': 21, 'episode_number': 81, 'genres': 'anime'}


class FinalPackFallback(unittest.TestCase):
    def _multi_seen(self, force):
        queue = ScrapingQueue()
        manager = Mock()
        manager.generate_identifier.return_value = 'One Piece S21E81'
        with patch('database.get_media_item_by_id', return_value={**ITEM, 'fall_back_to_single_scraper': 1}, create=True), \
                patch('queues.scraping_queue.scrape', return_value=([{'title': 'pack'}], [])) as scrape, \
                patch('scraper.scrape_status.get_unavailable', return_value=set()), \
                patch('database.database_writing.update_partial_scrape'), \
                patch('queues.scraping_queue.get_setting', return_value=30):
            queue.scrape_with_fallback(dict(ITEM), True, manager, skip_filter=True,
                                       check_pack_wantedness=False, force_multi_pack=force)
        return scrape.call_args.args[8]

    def test_the_flag_still_forces_single_mode_for_ordinary_calls(self):
        self.assertFalse(self._multi_seen(force=False))

    def test_the_final_fallback_keeps_pack_mode_despite_the_flag(self):
        self.assertTrue(self._multi_seen(force=True))


if __name__ == '__main__':
    unittest.main()
