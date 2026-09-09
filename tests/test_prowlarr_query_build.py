"""Tests for the Prowlarr query construction.

Two defects, both measured against the live indexer set on 2026-09-08:

1. The ID search sent ``query: ''``. No configured indexer advertises imdbId in
   its tvSearchParams, so every one of them ignored the ID and answered the
   empty search with its most recent ``limit`` entries -- the same 557-result
   index page came back for all 12 shows tested, with ~zero relevant hits, and
   it was roughly 74% of everything Prowlarr returned.

2. Anime was queried as SxxExx. Releases are named by absolute episode number,
   so that query returned 30 usable results from 817 raw across those 12 shows,
   and NOTHING AT ALL for 6 of them -- which is why they exhausted their retry
   budget and blacklisted.
"""
import unittest

import database  # noqa: F401  (circular import: must be imported first)

from scraper.prowlarr import _build_prowlarr_params_list


def _keyword_queries(*args):
    """Imported lazily so the empty-query tests below still RUN against a build
    that predates this helper, and fail on the assertion rather than on the
    import."""
    from scraper.prowlarr import _anime_keyword_queries
    return _anime_keyword_queries(*args)


def build(**kw):
    args = dict(title='One Piece', year=1999, content_type='episode',
                imdb_id='tt0388629', tmdb_id=None, season=19, episode=10,
                multi=False, tags_setting='')
    args.update(kw)
    try:
        return _build_prowlarr_params_list(**args)
    except TypeError:
        # A build predating episode_formats: drop it so the shared assertions
        # (never an empty query) still exercise the old code path.
        args.pop('episode_formats', None)
        return _build_prowlarr_params_list(**args)


class TestNeverAnEmptyQuery(unittest.TestCase):
    """The regression. Every params dict must carry a real search term."""

    def test_episode_id_search_carries_the_title(self):
        params = build()
        id_queries = [p for p in params if 'imdbId' in p]
        self.assertTrue(id_queries, "the ID search should still be issued")
        for p in id_queries:
            self.assertEqual(p['query'], 'One Piece')
            self.assertEqual(p['imdbId'], '0388629')

    def test_movie_id_search_carries_title_and_year(self):
        params = build(content_type='movie', title='Vegas Vacation', year=1997,
                       season=None, episode=None)
        id_queries = [p for p in params if 'imdbId' in p]
        self.assertTrue(id_queries)
        for p in id_queries:
            self.assertEqual(p['query'], 'Vegas Vacation 1997')

    def test_no_params_dict_ever_has_an_empty_query(self):
        for kwargs in (
            {},
            {'content_type': 'movie', 'season': None, 'episode': None},
            {'multi': True, 'episode': None},
            {'imdb_id': None, 'tmdb_id': '12345'},
            {'imdb_id': None, 'tmdb_id': None},
            {'content_type': 'other', 'season': None, 'episode': None},
        ):
            for p in build(**kwargs):
                self.assertTrue(
                    str(p.get('query') or '').strip(),
                    f"empty query emitted for {kwargs}: {p}")

    def test_a_blank_pack_format_cannot_produce_a_bare_query(self):
        """Season-pack formats include 'batch': '' -- an empty marker, not a term."""
        params = build(multi=True, episode=None,
                       episode_formats={'season': 'S19', 'batch': '', 'absolute': '892'})
        for p in params:
            self.assertTrue(str(p.get('query') or '').strip())
            self.assertNotEqual(p['query'].strip(), 'One Piece batch')


class TestAnimeAbsoluteNumbering(unittest.TestCase):
    def test_absolute_format_becomes_a_keyword_search(self):
        params = build(episode_formats={'regular': 'S19E10', 'absolute': '1069',
                                        'combined': 'S19E1069'})
        queries = [p['query'] for p in params if p['type'] == 'search']
        self.assertIn('One Piece 1069', queries)

    def test_combined_and_regular_are_not_queried(self):
        params = build(episode_formats={'regular': 'S19E10', 'absolute': '1069',
                                        'combined': 'S19E1069'})
        keyword = [p['query'] for p in params if p['type'] == 'search']
        self.assertNotIn('One Piece S19E1069', keyword)
        self.assertNotIn('One Piece S19E10', keyword)

    def test_keyword_searches_do_not_request_a_thousand_results(self):
        params = build(episode_formats={'absolute': '1069'})
        for p in params:
            if p['type'] == 'search':
                self.assertLess(p['limit'], 1000)

    def test_xem_orig_variants_are_deduplicated(self):
        params = build(episode_formats={'absolute': '1069', 'orig_absolute': '1069'})
        queries = [p['query'] for p in params if p['type'] == 'search']
        self.assertEqual(queries.count('One Piece 1069'), 1)

    def test_request_count_is_capped(self):
        formats = {f'absolute_{i}': str(1000 + i) for i in range(10)}
        self.assertLessEqual(len(_keyword_queries('One Piece', formats)), 2)

    def test_no_episode_formats_means_no_extra_queries(self):
        """A non-anime episode must be unchanged."""
        params = build(episode_formats=None)
        self.assertEqual([p for p in params if p['type'] == 'search'], [])

    def test_empty_and_missing_patterns_are_skipped(self):
        self.assertEqual(_keyword_queries('X', {'absolute': ''}), [])
        self.assertEqual(_keyword_queries('X', {'absolute': None}), [])
        self.assertEqual(_keyword_queries('X', {}), [])
        self.assertEqual(_keyword_queries('X', None), [])


class TestAlternateAbsolute(unittest.TestCase):
    """A dual-tree episode is asked for under both of its numbers."""

    FORMATS = {'regular': 'S18E119', 'absolute': '162', 'absolute_alt': '1061',
               'combined': 'S18E162', 'absolute_padded': '162'}

    def test_both_numbers_are_keyword_searches(self):
        queries = [p['query'] for p in build(season=18, episode=119, episode_formats=self.FORMATS)
                   if p['type'] == 'search']
        self.assertIn('One Piece 162', queries)
        self.assertIn('One Piece 1061', queries)

    def test_the_cap_still_holds(self):
        from scraper.prowlarr import _MAX_ANIME_KEYWORD_QUERIES
        self.assertLessEqual(len(_keyword_queries('One Piece', self.FORMATS)), _MAX_ANIME_KEYWORD_QUERIES)


class TestUnchangedBehaviour(unittest.TestCase):
    def test_title_text_search_still_uses_sxxexx(self):
        queries = [p['query'] for p in build() if p['type'] == 'tvsearch']
        self.assertIn('One Piece S19E10', queries)

    def test_structured_season_and_episode_are_still_sent(self):
        for p in build():
            if p['type'] == 'tvsearch':
                self.assertEqual(p['season'], 19)
                self.assertEqual(p['episode'], 10)

    def test_indexer_ids_from_tags_are_preserved(self):
        for p in build(tags_setting='4, 7', episode_formats={'absolute': '1069'}):
            self.assertEqual(p['indexerIds'], [4, 7])


if __name__ == '__main__':
    unittest.main()


class TestSearchTermSanitiser(unittest.TestCase):
    """rename_special_characters shipped with its body inside a docstring and
    returned its input unchanged; Zilean double-encodes anything that needs
    escaping, so punctuated and accented titles returned nothing (2,167 of
    2,167 non-ASCII queries on 2026-09-08)."""

    def _clean(self, s):
        from scraper.prowlarr import rename_special_characters
        return rename_special_characters(s)

    def test_multiplication_sign_becomes_x_not_nothing(self):
        # 'HUNTER HUNTER' finds a 2020 film; 'HUNTER x HUNTER' finds the show.
        self.assertEqual(self._clean('HUNTER×HUNTER'), 'HUNTER x HUNTER')

    def test_accents_colons_and_parentheses_are_stripped(self):
        self.assertEqual(self._clean('Pokémon: Indigo League (1998)'), 'Pokemon Indigo League 1998')
        self.assertEqual(self._clean('One Piece: Egghead Island (1089-Current)'), 'One Piece Egghead Island 1089-Current')
        self.assertEqual(self._clean("JoJo's Bizarre Adventure"), 'JoJos Bizarre Adventure')

    def test_plain_titles_are_untouched(self):
        self.assertEqual(self._clean('One Piece'), 'One Piece')
        self.assertEqual(self._clean('Bleach'), 'Bleach')

    def test_no_query_leaves_the_builder_with_a_trigger_character(self):
        from scraper.prowlarr import _build_prowlarr_params_list
        params = _build_prowlarr_params_list('Pokémon: Indigo League (1998)', 1998, 'episode',
                                             'tt0168366', None, 18, 53, False, '',
                                             episode_formats={'absolute': '1146'})
        self.assertTrue(params)
        for p in params:
            q = p.get('query', '')
            for ch in ':()é×':
                self.assertNotIn(ch, q, f"{ch!r} reached the query: {q!r}")
