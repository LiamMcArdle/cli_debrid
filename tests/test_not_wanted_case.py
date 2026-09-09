"""A bare hash in not_wanted must match its magnet regardless of letter case.

On 2026-09-09, 3,290 of 52,735 stored hashes were uppercase. get_base_filename
lowercased the btih it pulled out of a magnet but returned a bare stored hash
as-is, so those entries matched nothing and their torrents were re-added.
"""

import os
import pickle
import tempfile
import unittest
from unittest.mock import patch

import database  # noqa: F401  (circular-import guard, must come first)
import database.not_wanted_magnets as nw

UP = 'ABCDEF0123456789ABCDEF0123456789ABCDEF01'
LOW = UP.lower()


class _TempStore(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), 'not_wanted_magnets.pkl')
        self._p = [patch.object(nw, 'NOT_WANTED_MAGNETS_FILE', self.path),
                   patch.object(nw, 'get_setting', side_effect=lambda s, k, d=None: d)]
        for p in self._p: p.start()

    def tearDown(self):
        for p in self._p: p.stop()

    def _seed(self, values):
        with open(self.path, 'wb') as fh: pickle.dump(set(values), fh)


class TestCaseInsensitiveHashes(_TempStore):
    def test_an_uppercase_hash_written_now_matches_a_lowercase_magnet(self):
        nw.add_to_not_wanted(UP)
        self.assertTrue(nw.is_magnet_not_wanted('magnet:?xt=urn:btih:' + LOW))
        self.assertEqual(nw.load_not_wanted_magnets(), {LOW}, 'stored lowercase')

    def test_a_legacy_uppercase_entry_matches_after_the_boot_normalisation(self):
        self._seed([UP, LOW, 'not-a-hash', 'magnet:?xt=urn:btih:' + UP])
        nw.validate_not_wanted_entries()
        store = nw.load_not_wanted_magnets()
        self.assertIn(LOW, store)
        self.assertNotIn(UP, store, 'upper/lower duplicates collapsed')
        self.assertIn('not-a-hash', store, 'non-hash entries untouched')
        self.assertTrue(nw.is_magnet_not_wanted('magnet:?xt=urn:btih:' + LOW))
        self.assertTrue(nw.is_magnet_not_wanted('magnet:?xt=urn:btih:' + UP))

    def test_lookup_of_a_legacy_uppercase_entry_works_even_before_normalisation(self):
        self._seed([UP])
        self.assertTrue(nw.is_magnet_not_wanted('magnet:?xt=urn:btih:' + LOW),
                        'get_base_filename normalises the stored side too')

    def test_boot_normalisation_is_a_no_op_when_nothing_is_uppercase(self):
        self._seed([LOW, 'https://x/file.torrent'])
        mtime = os.path.getmtime(self.path)
        nw.validate_not_wanted_entries()
        self.assertEqual(os.path.getmtime(self.path), mtime, 'no rewrite when clean')


if __name__ == '__main__':
    unittest.main()
