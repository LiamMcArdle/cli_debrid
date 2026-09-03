"""The cross-pass file-claim query must not exceed SQLite's parameter cap.

A full series pack can list more than 999 files; one oversized IN list raised
and the guard failed open for exactly the packs it exists for.
"""

import unittest
from unittest.mock import MagicMock, patch

# database must finish initializing before queues.media_matcher is imported.
import database  # noqa: F401
from queues.media_matcher import MediaMatcher


class _Conn:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params):
        self.executed.append((sql, params))
        # Pretend every filename ending in '7.mkv' is already in use.
        return [(p,) for p in params[1:] if isinstance(p, str) and p.endswith('7.mkv')]

    def close(self):
        pass


class BasenameClaimChunking(unittest.TestCase):
    def test_large_lists_are_chunked_and_unioned(self):
        names = [f'{i}.mkv' for i in range(1200)]
        conn = _Conn()
        with patch('database.core.get_db_connection', return_value=conn):
            in_use = MediaMatcher()._basenames_already_in_use(names, 'tt1', exclude_item_id=9)
        self.assertEqual(len(conn.executed), 3)
        self.assertTrue(all(len(params) <= 502 for _, params in conn.executed))
        self.assertTrue(all(params[-1] == 9 for _, params in conn.executed))
        self.assertEqual(in_use, {n for n in names if n.endswith('7.mkv')})

    def test_small_list_is_one_query(self):
        conn = _Conn()
        with patch('database.core.get_db_connection', return_value=conn):
            MediaMatcher()._basenames_already_in_use(['a.mkv', 'b7.mkv'], 'tt1')
        self.assertEqual(len(conn.executed), 1)

    def test_empty_inputs_skip_the_database(self):
        with patch('database.core.get_db_connection') as get:
            self.assertEqual(MediaMatcher()._basenames_already_in_use([], 'tt1'), set())
            self.assertEqual(MediaMatcher()._basenames_already_in_use(['a'], None), set())
        get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
