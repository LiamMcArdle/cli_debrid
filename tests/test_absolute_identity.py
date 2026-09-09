"""The absolute episode number an anime search asks for, resolved by identity.

Every case here is a real row from the library audit of 2026-09-09. The
positional (season, episode) read of the battery returned 99 for Dragon Ball
Kai S2E1 (the row is episode 27), hid the second number Pokemon Sun & Moon
episodes are released under, and a draft that remapped by air date moved
Saint Seiya, Chiikawa and Slime rows onto their neighbours because the
library's dates were stale. Title evidence decides; dates only confirm.
"""
import importlib.util
import os
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'scraper', 'functions', 'season_resolution.py')
_spec = importlib.util.spec_from_file_location('season_resolution', _PATH)
_sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sr)

resolve = _sr.absolute_episode_from_identity


def row(season, episode, title, aired, absolute):
    return SimpleNamespace(season_number=season, episode_number=episode, title=title,
                           first_aired=aired, absolute_episode=absolute)


class TitleDecides(unittest.TestCase):
    def test_dragon_ball_kai_positional_99_is_episode_27(self):
        rows = [row(1, 27, 'A Touch-and-Go Situation! Gohan, Protect the Four Star Ball!',
                    '2009-10-11 03:00:00', 27),
                row(2, 1, 'Seven Years Later! Starting Today, Gohan is a High School Student',
                    '2014-04-06 03:00:00', 99)]
        self.assertEqual(
            resolve(rows, 2, 1, 'A Touch-and-Go Situation! Gohan, Protect the Four Star Ball!', '2009-10-10'),
            (27, _sr.ABS_REMAPPED_TITLE, ()))

    def test_positional_confirmed_by_title(self):
        rows = [row(1, 5, 'The Pendant', '2020-02-05 09:00:00', 5)]
        self.assertEqual(resolve(rows, 1, 5, 'The Pendant', '2020-02-05'),
                         (5, _sr.ABS_VERIFIED_TITLE, ()))

    def test_pokemon_dual_tree_is_ambiguous_even_though_the_positional_matches(self):
        """S18E119 sits at 162 on one tree and 1061 on the other; releases carry
        1061. The positional row's title matching must not end the search."""
        rows = [row(18, 119, 'A High-Speed Awakening!', '2019-04-28 09:45:00', 162),
                row(22, 27, 'A High-Speed Awakening!', '2019-04-28 09:55:00', 1061)]
        absolute, verdict, candidates = resolve(rows, 18, 119, 'A High-Speed Awakening!', '2019-04-28')
        self.assertEqual(verdict, _sr.ABS_AMBIGUOUS)
        self.assertEqual(candidates, (162, 1061))
        self.assertIn(absolute, candidates)

    def test_two_parter_matches_exactly_before_the_part_marker_is_stripped(self):
        rows = [row(21, 1, 'Enter Pikachu! (1)', '2019-11-17 09:30:00', 986),
                row(21, 2, 'Enter Pikachu! (2)', '2019-11-17 09:30:00', 987)]
        self.assertEqual(resolve(rows, 21, 1, 'Enter Pikachu! (1)', '2019-11-17'),
                         (986, _sr.ABS_VERIFIED_TITLE, ()))

    def test_part_marker_is_a_fallback_when_the_exact_title_is_absent(self):
        rows = [row(1, 3, 'The Gates of Warp! (1)', '2020-01-01', 3),
                row(2, 1, 'Something Else', '2021-01-01', 30)]
        self.assertEqual(resolve(rows, 2, 1, 'The Gates of Warp!', '2020-01-01'),
                         (3, _sr.ABS_REMAPPED_TITLE, ()))

    def test_dual_tree_broken_by_air_date(self):
        rows = [row(1, 3, 'Dreams and Reality', '2010-10-02 08:00:00', 1),
                row(4, 1, 'Dreams and Reality', '2013-01-05 08:00:00', 75)]
        self.assertEqual(resolve(rows, 4, 1, 'Dreams and Reality', '2013-01-05'),
                         (75, _sr.ABS_AMBIGUOUS_DATE, (1, 75)))

    def test_dual_tree_broken_by_estimate_when_dates_do_not_help(self):
        rows = [row(1, 3, 'Reused', '2010-10-02', 3),
                row(4, 1, 'Reused', '2010-10-02', 75)]
        self.assertEqual(resolve(rows, 4, 1, 'Reused', '2010-10-02', estimate=75),
                         (75, _sr.ABS_AMBIGUOUS_ESTIMATE, (3, 75)))

    def test_every_candidate_is_returned_when_nothing_breaks_the_tie(self):
        rows = [row(1, 29, 'Ancestors, Come On', '1979-04-27', 36),
                row(1, 1630, 'Ancestors, Come On', '2015-04-27', 1630)]
        absolute, verdict, candidates = resolve(rows, 1, 29, 'Ancestors, Come On', None, estimate=29)
        self.assertEqual((verdict, candidates), (_sr.ABS_AMBIGUOUS, (36, 1630)))
        self.assertEqual(absolute, 36)

    def test_consistent_dual_rows_at_one_absolute_are_not_ambiguous(self):
        rows = [row(1, 27, 'Same', '2009-10-11', 27), row(2, 1, 'Same', '2009-10-11', 27)]
        self.assertEqual(resolve(rows, 2, 1, 'Same', '2009-10-10'), (27, _sr.ABS_VERIFIED_TITLE, ()))

    def test_positional_row_absent_but_title_unique_elsewhere(self):
        rows = [row(1, 19, 'Air Combat', '1979-04-22', 19)]
        self.assertEqual(resolve(rows, 1, 15, 'Air Combat', '1979-04-17'),
                         (19, _sr.ABS_REMAPPED_TITLE, ()))


class DatesOnlyConfirm(unittest.TestCase):
    def test_saint_seiya_stale_date_does_not_move_to_the_neighbour(self):
        """Media title differs by a prefix; the media date matches E18's
        battery date. The draft remapped 16 -> 18. The row is episode 16."""
        rows = [row(1, 16, "Docrates' Fierce Attack", '1987-01-24', 16),
                row(1, 18, 'Great Rage! The Ghost Saints of Caribbean', '1987-02-07', 18)]
        self.assertEqual(resolve(rows, 1, 16, 'Gigantic! Docrates Fierce Attack', '1987-02-06'),
                         (16, _sr.ABS_POSITIONAL_UNCONFIRMED, ()))

    def test_generic_title_with_stale_date_keeps_the_positional(self):
        rows = [row(4, 17, 'Lubelius, the Point of Convergence', '2026-08-07', 89),
                row(4, 18, 'Turbulence in the West', '2026-08-14', 90)]
        self.assertEqual(resolve(rows, 4, 18, 'Episode 18', '2026-08-07'),
                         (90, _sr.ABS_POSITIONAL_UNCONFIRMED, ()))

    def test_two_battery_rows_on_the_date_never_become_candidates(self):
        rows = [row(2, 13, 'A Single Journey', '1991-09-12', 39),
                row(2, 15, 'Ragnarok', '1991-09-30', 41),
                row(2, 16, 'Thunder', '1991-09-30', 42)]
        self.assertEqual(resolve(rows, 2, 13, 'The Journey Begins', '1991-09-30'),
                         (39, _sr.ABS_POSITIONAL_UNCONFIRMED, ()))

    def test_positional_verified_by_date_when_titles_are_generic(self):
        rows = [row(1, 132, 'Performance / Are You Surprised', '2024-01-11 00:30:00', 132)]
        self.assertEqual(resolve(rows, 1, 132, 'Episode 132', '2024-01-10'),
                         (132, _sr.ABS_VERIFIED_DATE, ()))

    def test_no_positional_absolute_and_one_row_on_the_date_remaps(self):
        rows = [row(0, 3, 'Recap', '2020-05-05', None), row(1, 9, 'Nine', '2020-05-05', 9)]
        self.assertEqual(resolve(rows, 0, 3, 'Something Unmatched', '2020-05-05'),
                         (9, _sr.ABS_REMAPPED_DATE, ()))

    def test_batch_release_with_generic_titles_and_no_positional_is_unresolved(self):
        rows = [row(1, 1, 'Episode 1', '2019-03-01', 1), row(1, 2, 'Episode 2', '2019-03-01', 2),
                row(1, 3, 'Episode 3', '2019-03-01', 3)]
        self.assertEqual(resolve(rows, 2, 1, 'Episode 1', '2019-03-01'),
                         (None, _sr.ABS_UNRESOLVED, ()))

    def test_two_days_apart_is_not_the_same_day(self):
        rows = [row(1, 5, 'Five', '2020-02-05 00:30:00', 5)]
        self.assertEqual(resolve(rows, 1, 5, 'Episode 5', '2020-02-04')[1], _sr.ABS_VERIFIED_DATE)
        self.assertEqual(resolve(rows, 1, 5, 'Episode 5', '2020-02-03')[1], _sr.ABS_POSITIONAL_UNCONFIRMED)


class RowsAndDates(unittest.TestCase):
    def test_specials_and_rows_without_an_absolute_are_ignored(self):
        rows = [row(0, 1, 'Reused', '2020-01-01', 500),
                row(1, 4, 'Reused', '2020-01-01', None),
                row(1, 5, 'Reused', '2020-01-01', 5)]
        self.assertEqual(resolve(rows, 1, 5, 'Reused', '2020-01-01'), (5, _sr.ABS_VERIFIED_TITLE, ()))

    def test_no_identity_keeps_the_positional_unverified(self):
        rows = [row(1, 5, 'Five', '2020-02-05', 5)]
        for title, when in ((None, None), ('', 'Unknown'), ('Episode 5', None), ('TBA', '')):
            self.assertEqual(resolve(rows, 1, 5, title, when), (5, _sr.ABS_UNVERIFIED, ()))

    def test_no_rows(self):
        self.assertEqual(resolve([], 1, 5, 'Five', '2020-02-05'), (None, _sr.ABS_NO_ROWS, ()))
        self.assertEqual(resolve([row(1, 5, 'Five', '2020-02-05', None)], 1, 5, 'Five', '2020-02-05'),
                         (None, _sr.ABS_NO_ROWS, ()))

    def test_dict_rows_are_accepted(self):
        rows = [{'season_number': 1, 'episode_number': 5, 'title': 'Five',
                 'first_aired': '2020-02-05', 'absolute_episode': 5}]
        self.assertEqual(resolve(rows, 2, 1, 'Five', None), (5, _sr.ABS_REMAPPED_TITLE, ()))

    def test_media_date_forms(self):
        self.assertEqual(_sr.parse_local_date('2020-02-05'), date(2020, 2, 5))
        self.assertEqual(_sr.parse_local_date('2020-02-05 00:00:00'), date(2020, 2, 5))
        self.assertEqual(_sr.parse_local_date(date(2020, 2, 5)), date(2020, 2, 5))
        self.assertEqual(_sr.parse_local_date(datetime(2020, 2, 5, 23, 0)), date(2020, 2, 5))
        for bad in (None, '', 'Unknown', 'None', '2020-13-40', 42):
            self.assertIsNone(_sr.parse_local_date(bad))

    def test_battery_date_forms_use_the_utc_day(self):
        self.assertEqual(_sr.parse_utc_date('2019-04-28 09:45:00.000000'), date(2019, 4, 28))
        self.assertEqual(_sr.parse_utc_date('2019-04-28T00:30:00Z'), date(2019, 4, 28))
        self.assertEqual(_sr.parse_utc_date(datetime(2019, 4, 28, 0, 30, tzinfo=timezone.utc)),
                         date(2019, 4, 28))
        self.assertIsNone(_sr.parse_utc_date(None))

    def test_specific_titles(self):
        self.assertTrue(_sr.episode_title_is_specific('Myth'))
        self.assertTrue(_sr.episode_title_is_specific('THE WHITE HAZE'))
        for generic in ('Episode 18', 'Part 2', 'TBA', 'Special', '', None):
            self.assertFalse(_sr.episode_title_is_specific(generic))


import database  # noqa: E402,F401  (circular import: must be imported first)
from scraper import scraper as scraper_mod  # noqa: E402


class BatteryWrapper(unittest.TestCase):
    """One query, one log line, the identity verdict applied."""

    ROWS = [(27, 'A Touch-and-Go Situation! Gohan, Protect the Four Star Ball!', '2009-10-11 03:00:00', 27, 1),
            (1, 'Seven Years Later! Starting Today, Gohan is a High School Student', '2014-04-06 03:00:00', 99, 2)]

    def _session(self, item, rows):
        session = MagicMock()

        def query(*args):
            q = MagicMock()
            q.filter_by.return_value.first.return_value = item
            q.join.return_value.filter.return_value.all.return_value = rows
            return q
        session.query.side_effect = query
        session_cm = MagicMock()
        session_cm.__enter__.return_value = session
        session_cm.__exit__.return_value = False
        return session, session_cm

    def test_remapped_number_is_returned_and_logged(self):
        session, cm = self._session(SimpleNamespace(id=1), self.ROWS)
        with patch('cli_battery.app.database.Session', return_value=cm), \
                self.assertLogs(level='INFO') as logs:
            result = scraper_mod.get_absolute_episode_from_database(
                'tt1409055', 2, 1,
                episode_title='A Touch-and-Go Situation! Gohan, Protect the Four Star Ball!',
                release_date='2009-10-10')
        self.assertEqual(result, (27, ()))
        self.assertEqual(session.query.call_count, 2)
        self.assertTrue(any('tt1409055 S02E01: ABS_REMAPPED_TITLE (positional 99 -> 27)' in line
                            for line in logs.output), logs.output)

    def test_ambiguous_returns_the_alternates(self):
        rows = [(119, 'A High-Speed Awakening!', '2019-04-28 09:45:00', 162, 18),
                (27, 'A High-Speed Awakening!', '2019-04-28 09:55:00', 1061, 22)]
        _, cm = self._session(SimpleNamespace(id=1), rows)
        with patch('cli_battery.app.database.Session', return_value=cm), \
                self.assertLogs(level='INFO') as logs:
            absolute, alternates = scraper_mod.get_absolute_episode_from_database(
                'tt0168366', 18, 119, episode_title='A High-Speed Awakening!', release_date='2019-04-28')
        self.assertEqual({absolute, *alternates}, {162, 1061})
        self.assertEqual(len(alternates), 1)
        self.assertTrue(any('candidates=(162, 1061)' in line for line in logs.output), logs.output)

    def test_tiebroken_ambiguity_returns_no_alternate(self):
        rows = [(3, 'Dreams and Reality', '2010-10-02', 1, 1), (1, 'Dreams and Reality', '2013-01-05', 75, 4)]
        _, cm = self._session(SimpleNamespace(id=1), rows)
        with patch('cli_battery.app.database.Session', return_value=cm):
            self.assertEqual(scraper_mod.get_absolute_episode_from_database(
                'tt1', 4, 1, episode_title='Dreams and Reality', release_date='2013-01-05'), (75, ()))

    def test_unknown_show_and_missing_arguments(self):
        _, cm = self._session(None, [])
        with patch('cli_battery.app.database.Session', return_value=cm):
            self.assertEqual(scraper_mod.get_absolute_episode_from_database('tt0', 1, 1), (None, ()))
        self.assertEqual(scraper_mod.get_absolute_episode_from_database(None, 1, 1), (None, ()))
        self.assertEqual(scraper_mod.get_absolute_episode_from_database('tt1', None, 1), (None, ()))


class FormatsFromTheResolvedNumber(unittest.TestCase):
    COUNTS = {1: 26, 2: 26}

    def test_battery_number_is_used_without_another_lookup(self):
        with patch.object(scraper_mod, 'get_absolute_episode_from_database') as lookup:
            formats = scraper_mod.convert_anime_episode_format(
                2, 1, self.COUNTS, imdb_id='tt1', battery_absolute=27, battery_checked=True)
        lookup.assert_not_called()
        self.assertEqual(formats['absolute'], '27')
        self.assertEqual(formats['absolute_padded'], '027')
        self.assertNotIn('absolute_alt', formats)

    def test_alternate_follows_absolute_immediately(self):
        formats = scraper_mod.convert_anime_episode_format(
            18, 119, {}, imdb_id='tt1', battery_absolute=162, battery_absolute_alt=1061, battery_checked=True)
        keys = list(formats)
        self.assertEqual(keys[keys.index('absolute') + 1], 'absolute_alt')
        self.assertEqual(formats['absolute_alt'], '1061')

    def test_alternate_equal_to_the_absolute_is_dropped(self):
        formats = scraper_mod.convert_anime_episode_format(
            1, 5, {}, imdb_id='tt1', battery_absolute=5, battery_absolute_alt=5, battery_checked=True)
        self.assertNotIn('absolute_alt', formats)

    def test_unresolved_battery_falls_through_to_arithmetic(self):
        with patch.object(scraper_mod, 'get_absolute_episode_from_database') as lookup:
            formats = scraper_mod.convert_anime_episode_format(
                1, 5, self.COUNTS, imdb_id='tt1', battery_absolute=None, battery_absolute_alt=9,
                battery_checked=True)
        lookup.assert_not_called()
        self.assertEqual(formats['absolute'], '5')
        self.assertNotIn('absolute_alt', formats)

    def test_legacy_call_still_probes_the_battery_positionally(self):
        with patch.object(scraper_mod, 'get_absolute_episode_from_database', return_value=(31, ())) as lookup:
            formats = scraper_mod.convert_anime_episode_format(2, 5, self.COUNTS, imdb_id='tt1')
        lookup.assert_called_once_with('tt1', 2, 5)
        self.assertEqual(formats['absolute'], '31')

    def test_season_pack_formats_are_unchanged_by_the_kwargs(self):
        with patch('cli_battery.app.direct_api.DirectAPI.get_show_metadata', return_value=(None, None)):
            plain = scraper_mod.convert_anime_episode_format(2, 1, self.COUNTS, imdb_id='tt1', multi=True)
            with_kwargs = scraper_mod.convert_anime_episode_format(
                2, 1, self.COUNTS, imdb_id='tt1', multi=True,
                battery_absolute=99, battery_absolute_alt=27, battery_checked=True)
        self.assertEqual(plain, with_kwargs)


class ResolveOncePerScrape(unittest.TestCase):
    def test_helper_reads_the_row_identity_and_the_estimate(self):
        details = {'episode_title': 'From the row', 'release_date': '2014-10-23'}
        with patch.object(scraper_mod, 'get_episode_details', return_value=details), \
                patch.object(scraper_mod, 'get_absolute_episode_from_database',
                             return_value=(850, (1061,))) as lookup:
            self.assertEqual(scraper_mod._resolve_battery_absolute('tt1', 17, 47, None, {1: 82, 16: 45}),
                             (850, 1061))
        lookup.assert_called_once_with('tt1', 17, 47, episode_title='From the row',
                                       release_date='2014-10-23', estimate=82 + 45 + 47)

    def test_gate_title_wins_over_the_row_title(self):
        with patch.object(scraper_mod, 'get_episode_details', return_value={'episode_title': 'Row'}), \
                patch.object(scraper_mod, 'get_absolute_episode_from_database', return_value=(None, ())) as lookup:
            self.assertEqual(scraper_mod._resolve_battery_absolute('tt1', 1, 2, 'Gate', {}), (None, None))
        self.assertEqual(lookup.call_args.kwargs['episode_title'], 'Gate')

    def test_arithmetic_estimate(self):
        self.assertEqual(scraper_mod._arithmetic_absolute(3, 4, {1: 12, 2: 13, 3: 12, 'x': 5}), 29)
        self.assertEqual(scraper_mod._arithmetic_absolute(1, 4, {}), 4)
        self.assertIsNone(scraper_mod._arithmetic_absolute(None, 4, {}))


if __name__ == '__main__':
    unittest.main()
