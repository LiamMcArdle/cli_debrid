import sys
import types
import unittest
from unittest.mock import Mock, patch

from database.blacklist import is_restorable_blacklist_item

_config_manager = types.ModuleType('queues.config_manager')
_config_manager.get_version_settings = lambda *args, **kwargs: {}
_config_manager.load_config = lambda *args, **kwargs: {}
sys.modules.setdefault('queues.config_manager', _config_manager)

from queues.blacklisted_queue import BlacklistedQueue


class TestBlacklistRestoration(unittest.TestCase):
    def item(self, **overrides):
        item = {
            'id': 7,
            'title': 'Test',
            'state': 'Blacklisted',
            'ghostlisted': 0,
            'imdb_id': 'tt1234567',
            'tmdb_id': '123',
            'season_number': 2,
        }
        item.update(overrides)
        return item

    def test_manual_imdb_or_tmdb_entry_is_not_restorable(self):
        for protected_id in ('tt1234567', '123'):
            with self.subTest(protected_id=protected_id), patch(
                    'database.manual_blacklist.is_blacklisted',
                    side_effect=lambda value, season: value == protected_id):
                self.assertFalse(is_restorable_blacklist_item(self.item()))

    def test_manual_check_receives_season(self):
        with patch('database.manual_blacklist.is_blacklisted', return_value=False) as check:
            self.assertTrue(is_restorable_blacklist_item(self.item()))
        self.assertEqual(check.call_args_list[0].args, ('tt1234567', 2))
        self.assertEqual(check.call_args_list[1].args, ('123', 2))

    def test_ghostlisted_item_is_not_restorable(self):
        with patch('database.manual_blacklist.is_blacklisted') as check:
            self.assertFalse(is_restorable_blacklist_item(self.item(ghostlisted=1)))
        check.assert_not_called()

    def test_scheduled_path_keeps_protected_item_untouched(self):
        queue = BlacklistedQueue()
        manager = Mock()
        manager.generate_identifier.return_value = 'Test S02E01'
        with patch('database.blacklist.is_restorable_blacklist_item', return_value=False), \
                patch('queues.blacklisted_queue.update_blacklisted_date') as update_date:
            self.assertFalse(queue.unblacklist_item(manager, self.item()))
        update_date.assert_not_called()
        manager.move_to_wanted.assert_not_called()

    def test_scheduled_path_restores_eligible_item_once(self):
        queue = BlacklistedQueue()
        manager = Mock()
        manager.generate_identifier.return_value = 'Test S02E01'
        item = self.item()
        with patch('database.blacklist.is_restorable_blacklist_item', return_value=True), \
                patch('queues.blacklisted_queue.update_blacklisted_date') as update_date:
            self.assertTrue(queue.unblacklist_item(manager, item))
        update_date.assert_called_once_with(7, None)
        manager.move_to_wanted.assert_called_once_with(item, 'Blacklisted')


if __name__ == '__main__':
    unittest.main()
