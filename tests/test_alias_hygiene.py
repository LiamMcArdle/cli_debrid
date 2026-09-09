"""Which aliases are dropped BEFORE the query caps, because they only repeat
a search already being sent.

Measured on 2026-09-09 from Prowlarr's own history: 37% of a day's tvsearch
volume was arc/season names and 'Title (YYYY)' forms. 'Pokémon: Indigo
League' alone went out 1,698 times -- for season 16-18 items.
"""
import unittest

import database  # noqa: F401  (circular import: must be imported first)
from scraper.scraper import prune_query_aliases, select_query_aliases, _query_title_key

ONE_PIECE = ['One Piece (1998)', 'Wan Pisu', 'One Piece: Egghead Island (1089-Current)',
             'One Piece Log: Fish-Man Island Saga', 'ワンピース', 'One Piece']
SEASONS = {21: ['Egghead Island (1089-Current)'], 15: ['Log: Fish-Man Island Saga']}


class AliasHygiene(unittest.TestCase):
    def test_one_piece(self):
        self.assertEqual(prune_query_aliases(ONE_PIECE, 'One Piece', 1998, SEASONS, season=19),
                         ['Wan Pisu', 'ワンピース'])

    def test_the_scraped_seasons_own_name_is_kept(self):
        self.assertEqual(prune_query_aliases(ONE_PIECE, 'One Piece', 1998, SEASONS, season=21),
                         ['Wan Pisu', 'One Piece: Egghead Island (1089-Current)', 'ワンピース'])

    def test_pokemon_indigo_league(self):
        aliases = ['Pokémon: Indigo League', 'Pocket Monsters', 'Pokémon']
        titles = {1: ['Indigo League']}
        self.assertEqual(prune_query_aliases(aliases, 'Pokemon', 1997, titles, season=17), ['Pocket Monsters'])
        self.assertEqual(prune_query_aliases(aliases, 'Pokemon', 1997, titles, season=1),
                         ['Pokémon: Indigo League', 'Pocket Monsters'])

    def test_a_different_year_is_a_different_show(self):
        self.assertEqual(prune_query_aliases(['Dragon Ball (1986)', 'Dragon Ball (2026)'], 'Dragon Ball', 1986, {}),
                         ['Dragon Ball (2026)'])

    def test_special_character_forms_collapse(self):
        self.assertEqual(prune_query_aliases(['Hunter × Hunter', 'Hunter x Hunter', 'HxH'], 'HUNTER×HUNTER', 2011, {}),
                         ['HxH'])

    def test_empty_inputs(self):
        self.assertEqual(prune_query_aliases([], 'T', 2000, {}), [])
        self.assertEqual(prune_query_aliases(None, 'T', None, None), [])
        self.assertEqual(prune_query_aliases(['', None, 'T'], 'T', 2000, {}), [])

    def test_order_is_preserved_and_the_caps_still_apply(self):
        aliases = ['B', 'A', 'C', 'D', 'A']
        pruned = prune_query_aliases(aliases, 'T', 2000, {})
        self.assertEqual(pruned, ['B', 'A', 'C', 'D'])
        latin, _ = select_query_aliases(pruned, set(), 3, 1)
        self.assertEqual(latin, ['B', 'A', 'C'])

    def test_key(self):
        self.assertEqual(_query_title_key('HUNTER×HUNTER'), _query_title_key('Hunter x Hunter'))
        self.assertEqual(_query_title_key('Pokémon: Indigo League'), 'pokemon indigo league')
        self.assertNotEqual(_query_title_key('ワンピース'), '')


if __name__ == '__main__':
    unittest.main()
