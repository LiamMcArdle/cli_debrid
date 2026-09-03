"""Which aliases become scrape QUERIES, as opposed to matching aliases.

Matching wants every name from every country. Querying wants a few Latin
names for the indexers everyone publishes to, plus the origin country's own
native-script name for Nyaa's raws -- never a spin-off's name, and never a
Korean, Hebrew or Cyrillic name that no indexer answers in.
"""

import unittest

# database must finish initializing before anything under scraper/ is imported.
import database  # noqa: F401
from scraper.scraper import select_query_aliases


HXH = [
    'Hunter x Hunter',
    'Hunter X Hunter: Greed Island (OAV)',
    'Hunter X Hunter: G I Final (OAV)',
    'Hunter X Hunter (OAV)',
    'ハンター×ハンター',
    '헌터X헌터',
    'האנטר X האנטר',
    'Hunter × Hunter',
    'HxH',
]
ORIGIN = {'ハンター×ハンター', 'Hunter x Hunter'}


class QueryAliasSelection(unittest.TestCase):
    def test_latin_capped_and_spinoffs_dropped(self):
        latin, native = select_query_aliases(HXH, ORIGIN, max_latin=3, max_native=1)
        self.assertEqual(latin, ['Hunter x Hunter', 'Hunter × Hunter', 'HxH'])
        self.assertNotIn('Hunter X Hunter (OAV)', latin)

    def test_native_limited_to_origin_country(self):
        _, native = select_query_aliases(HXH, ORIGIN, max_latin=3, max_native=2)
        self.assertEqual(native, ['ハンター×ハンター'])   # Korean and Hebrew never qualify

    def test_slots_do_not_cross_over(self):
        # One native slot free and plenty of Latin names: Latin stays at its cap.
        latin, native = select_query_aliases(HXH, set(), max_latin=2, max_native=3)
        self.assertEqual(len(latin), 2)
        self.assertEqual(native, [])

    def test_zero_caps_send_nothing(self):
        self.assertEqual(select_query_aliases(HXH, ORIGIN, 0, 0), ([], []))

    def test_spinoff_suffix_forms(self):
        aliases = ['Show Movie', 'Show OVA', 'Show Gekijouban', 'Show (Specials)', 'Show TV', 'Show Special Edition']
        latin, _ = select_query_aliases(aliases, set(), 10, 0)
        # 'Show TV' is not a spin-off marker and 'Special Edition' is a release
        # descriptor rather than a trailing marker; both are kept.
        self.assertEqual(latin, ['Show TV', 'Show Special Edition'])

    def test_empty_inputs(self):
        self.assertEqual(select_query_aliases([], None, 3, 1), ([], []))
        self.assertEqual(select_query_aliases([None, ''], None, 3, 1), ([], []))

    def test_preserves_order_and_uniqueness_of_input(self):
        latin, _ = select_query_aliases(['B', 'A', 'B'], set(), 3, 1)
        self.assertEqual(latin, ['B', 'A', 'B'][:3])


if __name__ == '__main__':
    unittest.main()
