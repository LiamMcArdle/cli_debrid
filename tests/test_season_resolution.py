"""Tests for the single season-resolution authority.

Cases are drawn from real rows that were mis-filled in the library, so a
regression here is a regression that already happened once.
"""
import importlib.util
import os
import unittest

# Loaded straight from its path rather than as scraper.functions.season_resolution:
# the package __init__ drags in PTT, the database layer and settings, and this
# module is deliberately free of all of them. Importing it in isolation is part
# of what is being asserted.
_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'scraper', 'functions', 'season_resolution.py')
_spec = importlib.util.spec_from_file_location('season_resolution', _PATH)
_sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sr)

season_verdict = _sr.season_verdict
container_season_from_path = _sr.container_season_from_path
EXPLICIT_MATCH = _sr.EXPLICIT_MATCH
EXPLICIT_MISMATCH = _sr.EXPLICIT_MISMATCH
SPECIALS = _sr.SPECIALS
CONTAINER_MATCH = _sr.CONTAINER_MATCH
CONTAINER_MISMATCH = _sr.CONTAINER_MISMATCH
SEASON_ONE_DEFAULT = _sr.SEASON_ONE_DEFAULT
ABSOLUTE_MATCH = _sr.ABSOLUTE_MATCH
ABSOLUTE_MISMATCH = _sr.ABSOLUTE_MISMATCH
ABSOLUTE_UNKNOWN = _sr.ABSOLUTE_UNKNOWN

episode_title_verdict = _sr.episode_title_verdict
episode_title_is_usable = _sr.episode_title_is_usable
normalize_title_text = _sr.normalize_title_text
episode_identity_verdict = _sr.episode_identity_verdict
TITLE_MATCH = _sr.TITLE_MATCH
TITLE_NOT_DISTINCTIVE = _sr.TITLE_NOT_DISTINCTIVE
TITLE_ABSENT = _sr.TITLE_ABSENT
TITLE_AMBIGUOUS = _sr.TITLE_AMBIGUOUS

# Demon Slayer: S1=26, S2=7, S3=11, S4=11, S5=8
DS_ABS = {1: 0, 2: 26, 3: 33, 4: 44, 5: 55}


def ds_absolute(season, episode):
    return DS_ABS[season] + episode


class TestTheReportedBug(unittest.TestCase):
    """'[SCY] Demon Slayer - 04.mkv' is season 1 episode 4 and nothing else."""

    def test_bare_number_rejected_for_every_later_season(self):
        for season in (2, 3, 4, 5):
            ok, reason = season_verdict(
                file_seasons=[], target_season=season, file_numbers=[4],
                absolute_episode=ds_absolute(season, 4), is_anime=True)
            self.assertFalse(ok, f"S{season}E04 wrongly accepted a bare '- 04' file")
            self.assertEqual(reason, ABSOLUTE_MISMATCH)

    def test_bare_number_still_fills_season_one(self):
        ok, reason = season_verdict(
            file_seasons=[], target_season=1, file_numbers=[4],
            absolute_episode=4, is_anime=True)
        self.assertTrue(ok)
        self.assertEqual(reason, SEASON_ONE_DEFAULT)

    def test_bilibili_s01_pack_rejected_for_s04(self):
        """The folder names S01, so S04E04 must not take a file from it."""
        path = ('Kimetsu no Yaiba (Demon Slayer) - S01 (2160p) (Bilibili)/'
                'Kimetsu no Yaiba (Demon Slayer) - 04 (2160p) (Bilibili).mkv')
        ok, reason = season_verdict(
            file_seasons=[], target_season=4, file_numbers=[4],
            absolute_episode=ds_absolute(4, 4),
            container_season=container_season_from_path(path), is_anime=True)
        self.assertFalse(ok)
        self.assertEqual(reason, CONTAINER_MISMATCH)


class TestFailOpen(unittest.TestCase):
    """An unknown absolute episode must never degrade to the plain number."""

    def test_unknown_absolute_rejects_for_s2_plus(self):
        ok, reason = season_verdict(
            file_seasons=[], target_season=4, file_numbers=[4],
            absolute_episode=None, is_anime=True)
        self.assertFalse(ok)
        self.assertEqual(reason, ABSOLUTE_UNKNOWN)

    def test_unknown_absolute_still_allows_season_one(self):
        ok, _ = season_verdict(
            file_seasons=[], target_season=1, file_numbers=[4],
            absolute_episode=None, is_anime=True)
        self.assertTrue(ok)


class TestLegitimateMatchesStillPass(unittest.TestCase):
    """The strictness must not cost us correct fills."""

    def test_absolute_numbered_release_matches(self):
        """'One Piece - 1089.mkv' for S21E1089 stored absolutely."""
        ok, reason = season_verdict(
            file_seasons=[], target_season=21, file_numbers=[1089],
            absolute_episode=1089, is_anime=True)
        self.assertTrue(ok)
        self.assertEqual(reason, ABSOLUTE_MATCH)

    def test_absolute_numbered_demon_slayer(self):
        """A real absolute release for S05E04 carries 59, not 4."""
        ok, reason = season_verdict(
            file_seasons=[], target_season=5, file_numbers=[59],
            absolute_episode=ds_absolute(5, 4), is_anime=True)
        self.assertTrue(ok)
        self.assertEqual(reason, ABSOLUTE_MATCH)

    def test_explicit_season_match(self):
        ok, reason = season_verdict(
            file_seasons=[3], target_season=3, file_numbers=[9],
            absolute_episode=42, is_anime=True)
        self.assertTrue(ok)
        self.assertEqual(reason, EXPLICIT_MATCH)

    def test_folder_names_the_right_season(self):
        path = 'Demon Slayer S04 2160p WEB H.265/Demon Slayer - 04.mkv'
        ok, reason = season_verdict(
            file_seasons=[], target_season=4, file_numbers=[4],
            absolute_episode=ds_absolute(4, 4),
            container_season=container_season_from_path(path), is_anime=True)
        self.assertTrue(ok)
        self.assertEqual(reason, CONTAINER_MATCH)

    def test_ptt_default_season_one_is_not_a_claim_for_anime(self):
        """PTT reports [1] for many bare anime titles; a real absolute-numbered
        release must not be rejected just because of that default."""
        ok, reason = season_verdict(
            file_seasons=[1], target_season=5, file_numbers=[59],
            absolute_episode=ds_absolute(5, 4), is_anime=True)
        self.assertTrue(ok)
        self.assertEqual(reason, ABSOLUTE_MATCH)

    def test_ptt_default_season_one_still_blocks_in_season_numbering(self):
        """...but the same default must not let season 1 content through."""
        ok, reason = season_verdict(
            file_seasons=[1], target_season=5, file_numbers=[4],
            absolute_episode=ds_absolute(5, 4), is_anime=True)
        self.assertFalse(ok)
        self.assertEqual(reason, ABSOLUTE_MISMATCH)

    def test_special_target_is_lenient(self):
        """Specials metadata disagrees everywhere, so a season-0 TARGET stays lenient."""
        ok, reason = season_verdict(
            file_seasons=[2], target_season=0, file_numbers=[2],
            absolute_episode=None, is_anime=True)
        self.assertTrue(ok)
        self.assertEqual(reason, SPECIALS)

    def test_special_file_cannot_fill_a_real_season(self):
        """'Ace.of.Diamond.S00E04' must not be chosen for S04E04."""
        ok, reason = season_verdict(
            file_seasons=[0], target_season=4, file_numbers=[4],
            absolute_episode=142, is_anime=True)
        self.assertFalse(ok)
        self.assertEqual(reason, EXPLICIT_MISMATCH)


class TestNonAnime(unittest.TestCase):
    def test_explicit_mismatch_rejected(self):
        ok, reason = season_verdict(
            file_seasons=[1], target_season=8, file_numbers=[10],
            absolute_episode=None, is_anime=False)
        self.assertFalse(ok)
        self.assertEqual(reason, EXPLICIT_MISMATCH)

    def test_season_one_is_not_a_claim_only_for_anime(self):
        """Non-anime keeps [1] as a real claim."""
        ok, reason = season_verdict(
            file_seasons=[1], target_season=1, file_numbers=[3],
            absolute_episode=None, is_anime=False)
        self.assertTrue(ok)
        self.assertEqual(reason, EXPLICIT_MATCH)

    def test_bare_number_not_allowed_into_s2_plus(self):
        ok, reason = season_verdict(
            file_seasons=[], target_season=4, file_numbers=[4],
            absolute_episode=None, is_anime=False)
        self.assertFalse(ok)
        self.assertEqual(reason, ABSOLUTE_UNKNOWN)


class TestStrictMode(unittest.TestCase):
    def test_strict_rejects_bare_season_one(self):
        ok, reason = season_verdict(
            file_seasons=[], target_season=1, file_numbers=[4],
            absolute_episode=4, is_anime=False, allow_bare_season_one=False)
        self.assertFalse(ok)

    def test_strict_still_accepts_explicit(self):
        ok, _ = season_verdict(
            file_seasons=[1], target_season=1, file_numbers=[4],
            absolute_episode=4, is_anime=False, allow_bare_season_one=False)
        self.assertTrue(ok)


class TestContainerSeasonParsing(unittest.TestCase):
    def test_single_season_folder(self):
        self.assertEqual(
            container_season_from_path('Show - S01 (1080p)/ep.mkv'), 1)

    def test_word_form(self):
        self.assertEqual(
            container_season_from_path('Show Season 3 [BD]/ep.mkv'), 3)

    def test_multi_season_pack_is_undecidable(self):
        for folder in ('Show S01+S02', 'Show S01-S05', 'Show Complete Series',
                       'Show Seasons 1-9', 'Attack on Titan S01-S04 + OVA',
                       'Futurama Season 1-11 Colection 1080p WEBDL'):
            self.assertIsNone(
                container_season_from_path(folder + '/ep.mkv'),
                f"{folder!r} should not resolve to a single season")

    def test_batch_is_undecidable(self):
        self.assertIsNone(
            container_season_from_path('[SCY] Demon Slayer (BD) [Batch]/ep.mkv'))

    def test_no_folder(self):
        self.assertIsNone(container_season_from_path('ep.mkv'))
        self.assertIsNone(container_season_from_path(''))


class TestEpisodeTitleNormalization(unittest.TestCase):

    def test_apostrophes_collapse_rather_than_split(self):
        # "time s the charm" would never match a release spelling it "Times".
        self.assertEqual(normalize_title_text("Third Time's the Charm"),
                         'third times the charm')
        self.assertEqual(normalize_title_text('Third Time’s the Charm'),
                         'third times the charm')

    def test_punctuation_becomes_a_single_space(self):
        self.assertEqual(normalize_title_text('Death, Destruction, Despair'),
                         'death destruction despair')
        self.assertEqual(normalize_title_text('S01E08-Who Killed Cock Robin [A38F14E4].mkv'),
                         's01e08 who killed cock robin a38f14e4 mkv')


class TestEpisodeTitleIsUsable(unittest.TestCase):

    def test_real_danganronpa_titles_all_usable(self):
        for title in ("Third Time's the Charm", 'Hang the Witch',
                      'Cruel Violence and Hollow Words', 'Who Is a Liar',
                      'Dreams of Distant Days', 'No Man Is an Island',
                      'Ultra Despair Girls', 'Who Killed Cock Robin?',
                      'You Are My Reason to Die', 'Death, Destruction, Despair',
                      'All Good Things', 'It Is Always Darkest'):
            self.assertTrue(episode_title_is_usable(title), title)

    def test_placeholder_titles_rejected(self):
        # Bleach S02E41's real title. Matching on this would be catastrophic.
        for title in ('Episode 41', 'Episode 9', 'Part 2', 'TBA', 'Untitled',
                      'Special 1', 'Chapter Three'):
            self.assertFalse(episode_title_is_usable(title), title)

    def test_short_or_single_word_titles_rejected(self):
        for title in ('Garbage', 'Unforgiven', 'Clash!', 'Outwit'):
            self.assertFalse(episode_title_is_usable(title), title)


class TestEpisodeTitleVerdict(unittest.TestCase):
    """The EMBER pack labels four different Danganronpa series S01Exx, so the
    episode title is the only thing that separates four distinct episodes."""

    EMBER = [
        "S01E01-Hello Again, Hope's Peak High School [3A7E8C12].mkv",
        'S01E01-The School of Hope and the Students of Despair [1E6347F2].mkv',
        "S01E01-Third Time's the Charm [56BECC03].mkv",
        'S01E01-Welcome to Despair High School [3D812AF5].mkv',
    ]

    def test_picks_the_one_file_that_names_the_title(self):
        others = ['Hello Again, Hope\'s Peak High School',
                  'The School of Hope and the Students of Despair',
                  'Welcome to Despair High School']
        hits = [f for f in self.EMBER
                if episode_title_verdict("Third Time's the Charm", f, others)[0]]
        self.assertEqual(hits, ["S01E01-Third Time's the Charm [56BECC03].mkv"])

    def test_absent_title_is_not_a_match(self):
        ok, reason = episode_title_verdict('Hang the Witch', self.EMBER[0], [])
        self.assertFalse(ok)
        self.assertEqual(reason, TITLE_ABSENT)

    def test_generic_title_never_matches_even_when_present(self):
        ok, reason = episode_title_verdict('Episode 41', 'Bleach - Episode 41.mkv', [])
        self.assertFalse(ok)
        self.assertEqual(reason, TITLE_NOT_DISTINCTIVE)

    def test_duplicate_title_in_another_season_is_ambiguous(self):
        # Same show, two episodes genuinely sharing a name: nothing in the
        # filename says which one it is.
        ok, reason = episode_title_verdict(
            'The Long Way Home', 'Show.S01E05.The.Long.Way.Home.mkv',
            other_episode_titles=['The Long Way Home'])
        self.assertFalse(ok)
        self.assertEqual(reason, TITLE_AMBIGUOUS)

    def test_episode_named_after_its_show_is_not_evidence(self):
        ok, reason = episode_title_verdict(
            'Kingdom Season Three', 'Kingdom Season Three - 04.mkv', [],
            series_title='Kingdom Season Three')
        self.assertFalse(ok)
        self.assertEqual(reason, TITLE_NOT_DISTINCTIVE)

    def test_word_boundaries_are_respected(self):
        # 'All Good Things' must not match 'All Good Thingsomething'.
        self.assertFalse(episode_title_verdict(
            'All Good Things', 'S01E11-All Good Thingsxyz.mkv', [])[0])


class TestPokemonTwoParter(unittest.TestCase):
    """Pokemon S24E41 'The Gates of Warp! (1)' and S24E42 'Showdown at the
    Gates of Warp! (2)': one title is a substring of the other, and the file on
    disk carries neither part marker."""

    FILE = 'Pokemon S19E90 Showdown at the Gates of Warp.mkv'
    E41 = 'The Gates of Warp! (1)'
    E42 = 'Showdown at the Gates of Warp! (2)'

    def test_part_marker_stripped_so_the_longer_title_matches(self):
        ok, reason = episode_title_verdict(self.E42, self.FILE, [self.E41])
        self.assertTrue(ok)
        self.assertEqual(reason, TITLE_MATCH)

    def test_shorter_title_must_not_steal_the_longer_ones_file(self):
        ok, reason = episode_title_verdict(self.E41, self.FILE, [self.E42])
        self.assertFalse(ok)
        self.assertEqual(reason, TITLE_AMBIGUOUS)

    def test_part_one_still_matches_its_own_file(self):
        ok, _ = episode_title_verdict(
            self.E41, 'Pokemon S19E89 The Gates of Warp.mkv', [self.E42])
        self.assertTrue(ok)


class TestTitleMatchingDoesNotReopenTheOriginalBug(unittest.TestCase):
    """The bug that started all this: a bare-numbered season-1 batch filling
    every season. None of those filenames carry an episode title, so the escape
    hatch cannot fire for them."""

    def test_bare_numbered_file_has_no_title_to_match(self):
        for title in ('Hashira Training', 'Swordsmith Village Arc'):
            self.assertFalse(episode_title_verdict(
                title, '[SCY] Demon Slayer - 04.mkv', [])[0])

    def test_wrong_season_file_of_the_same_show_is_not_rescued(self):
        self.assertFalse(episode_title_verdict(
            'Ultimate Lifeform', 'One Punch Man S01E02 - Strategy Meeting.mkv',
            other_episode_titles=['Strategy Meeting'])[0])


class TestEpisodeIdentityVerdict(unittest.TestCase):
    def verdict(self, filename, seasons, episodes, coordinates=None, absolute=81,
                episode_title=None, other_titles=None, max_absolute=None):
        return episode_identity_verdict(
            target_coordinates=coordinates or [(4, 3)],
            file_seasons=seasons,
            file_numbers=episodes,
            filename=filename,
            absolute_episode=absolute,
            max_absolute_episode=max_absolute,
            is_anime=True,
            episode_title=episode_title,
            other_episode_titles=other_titles or [],
            series_title='Hunter x Hunter',
        )

    def test_hunter_x_hunter_wrong_season_one_file_is_rejected(self):
        ok, reason = self.verdict(
            '[VEGETA] Hunter X Hunter (1999) - S01E03 [720p].mp4', [1], [3])
        self.assertFalse(ok)
        self.assertEqual(reason, _sr.IDENTITY_EXPLICIT_CONFLICT)

    def test_hunter_x_hunter_absolute_81_is_accepted(self):
        ok, reason = self.verdict(
            '[Samir755] Hunter x Hunter - 81 - An Encounter x Chrollo.mkv',
            [], [81])
        self.assertTrue(ok)
        self.assertEqual(reason, _sr.IDENTITY_ABSOLUTE)

    def test_hunter_x_hunter_year_does_not_hide_delimited_absolute_81(self):
        """PTT returns no episode for this exact live Torrentio filename."""
        ok, reason = self.verdict(
            '[Samir755] Hunter X Hunter 1999 -81- An Encounter x Chrollo.mkv',
            [], [])
        self.assertTrue(ok)
        self.assertEqual(reason, _sr.IDENTITY_ABSOLUTE)

    def test_delimited_in_season_number_is_not_an_absolute_match(self):
        ok, reason = self.verdict(
            '[VEGETA] Hunter X Hunter (1999) - 03 - Wrong Episode.mkv', [], [])
        self.assertFalse(ok)
        self.assertEqual(reason, _sr.IDENTITY_MISSING)

    def test_other_adaptation_batch_beyond_known_series_is_rejected(self):
        ok, reason = self.verdict(
            '[Erai-raws] Hunter X Hunter - 01 ~ 148 [1080p]',
            [1], list(range(1, 149)), max_absolute=92)
        self.assertFalse(ok)
        self.assertEqual(reason, _sr.IDENTITY_BEYOND_SERIES)

    def test_batch_ending_at_known_series_extent_is_still_eligible(self):
        ok, reason = self.verdict(
            'Hunter X Hunter (1999) - 01 ~ 92 [1080p]',
            [1], list(range(1, 93)), max_absolute=92)
        self.assertTrue(ok)
        self.assertEqual(reason, _sr.IDENTITY_COORDINATE)

    def test_s01e_absolute_notation_is_accepted_for_later_anime_season(self):
        ok, reason = self.verdict(
            '[Judas] Hunter x Hunter (2011) - S01E112.mkv',
            [1], [112], coordinates=[(2, 112)], absolute=112)
        self.assertTrue(ok)
        self.assertEqual(reason, _sr.IDENTITY_ABSOLUTE)

    def test_s01e_wrong_absolute_notation_is_rejected(self):
        ok, reason = self.verdict(
            '[Judas] Hunter x Hunter (1999) - S01E03.mkv',
            [1], [3], coordinates=[(4, 3)], absolute=81)
        self.assertFalse(ok)
        self.assertEqual(reason, _sr.IDENTITY_EXPLICIT_CONFLICT)

    def test_selected_episode_title_can_identify_unreconciled_numbering(self):
        ok, reason = self.verdict(
            '[WIP] Hunter x Hunter - 03 - An Encounter x Kuroro x The Gold Dust Girl.mkv',
            [], [3],
            episode_title='An Encounter x Kuroro x The Gold Dust Girl',
            other_titles=['Masadora x Big Strides x Mad Bomber'])
        self.assertTrue(ok)
        self.assertEqual(reason, _sr.IDENTITY_TITLE)

    def test_coordinate_alternatives_are_atomic(self):
        # Stored S01E25 and mapped S02E01 are both legitimate pairs.  S02E25
        # is the Cartesian combination the old independent fallbacks admitted.
        ok, reason = self.verdict(
            'Example.Show.S02E25.mkv', [2], [25],
            coordinates=[(2, 1), (1, 25)], absolute=None)
        self.assertFalse(ok)
        self.assertEqual(reason, _sr.IDENTITY_EXPLICIT_CONFLICT)

    def test_complete_stored_pair_remains_a_legacy_compatible_alternative(self):
        ok, reason = self.verdict(
            'Example.Show.S01E25.mkv', [1], [25],
            coordinates=[(2, 1), (1, 25)], absolute=None)
        self.assertTrue(ok)
        self.assertEqual(reason, _sr.IDENTITY_COORDINATE)


if __name__ == '__main__':
    unittest.main(verbosity=2)


# --- Wave 2 identity-gate rework -------------------------------------------

explicit_coordinates = _sr.explicit_coordinates
IDENTITY_SEASON_TITLE = _sr.IDENTITY_SEASON_TITLE
IDENTITY_MOVIE = _sr.IDENTITY_MOVIE
IDENTITY_FRACTIONAL = _sr.IDENTITY_FRACTIONAL
IDENTITY_ABSOLUTE = _sr.IDENTITY_ABSOLUTE
IDENTITY_COORDINATE = _sr.IDENTITY_COORDINATE
IDENTITY_MISSING = _sr.IDENTITY_MISSING
IDENTITY_EXPLICIT_CONFLICT = _sr.IDENTITY_EXPLICIT_CONFLICT
IDENTITY_BEYOND_SERIES = _sr.IDENTITY_BEYOND_SERIES


class ExplicitAnimeSeasonMarkerGuards(unittest.TestCase):
    """'S01 - <number>' is a coordinate only when the number is an episode."""

    def test_bit_depth_channel_layout_and_year_are_not_episodes(self):
        self.assertEqual(explicit_coordinates('Show - S01 - 10bit - 05.mkv'), [])
        self.assertEqual(explicit_coordinates('Show S01 - 5.1 - 03.mkv'), [])
        self.assertEqual(explicit_coordinates('Show S01 - 2019 - 03.mkv'), [])
        self.assertEqual(explicit_coordinates('Show S01 - 60fps.mkv'), [])

    def test_real_marker_still_reads(self):
        self.assertEqual(explicit_coordinates('Show - S02 - 07.mkv'), [(2, 7)])
        self.assertEqual(explicit_coordinates('One Piece S21 - 1000.mkv'), [(21, 1000)])


class ContainerSeasonBatchFolders(unittest.TestCase):
    def test_single_season_batch_folder_names_its_season(self):
        self.assertEqual(container_season_from_path('Show S02 Batch (1080p)/Show - 03.mkv'), 2)
        self.assertEqual(container_season_from_path('[Grp] Show Season 3 [Complete]/Show - 03.mkv'), 3)

    def test_spans_and_multi_season_folders_stay_ambiguous(self):
        self.assertIsNone(container_season_from_path('Show S01-S03 Complete/Show - 03.mkv'))
        self.assertIsNone(container_season_from_path('Show Season 1 to 3/Show - 03.mkv'))
        self.assertIsNone(container_season_from_path('Show Seasons 1-5/Show - 03.mkv'))
        self.assertIsNone(container_season_from_path('Show Complete/Show - 03.mkv'))


class SeasonTitleTier(unittest.TestCase):
    TITLES = {17: ['Thousand-Year Blood War'],
              18: ['Thousand-Year Blood War - The Separation']}

    def _verdict(self, filename, target=(17, 1), container=None, numbers=(1,)):
        return episode_identity_verdict(
            target_coordinates=[target], file_seasons=[], file_numbers=list(numbers),
            filename=filename, absolute_episode=367, is_anime=True,
            series_title='Bleach', season_titles=self.TITLES, container_text=container)

    def test_arc_named_release_with_restart_numbering_is_the_season(self):
        ok, reason = self._verdict('[SubsPlease] Bleach - Thousand-Year Blood War - 01 (1080p).mkv')
        self.assertTrue(ok)
        self.assertEqual(reason, IDENTITY_SEASON_TITLE)

    def test_longer_sibling_title_wins(self):
        # 'The Separation' names S18; its file must not satisfy S17.
        ok, _ = self._verdict('Bleach - Thousand-Year Blood War - The Separation - 01.mkv', target=(17, 1))
        self.assertFalse(ok)
        ok, reason = self._verdict('Bleach - Thousand-Year Blood War - The Separation - 01.mkv', target=(18, 1))
        self.assertTrue(ok)
        self.assertEqual(reason, IDENTITY_SEASON_TITLE)

    def test_bare_number_is_not_promoted(self):
        ok, reason = self._verdict('[SubsPlease] Bleach - 01 (1080p).mkv')
        self.assertFalse(ok)
        self.assertEqual(reason, IDENTITY_MISSING)

    def test_explicit_coordinate_still_outranks_the_title(self):
        ok, reason = self._verdict('Bleach TYBW S01E01 - Thousand-Year Blood War.mkv')
        self.assertFalse(ok)
        self.assertEqual(reason, IDENTITY_EXPLICIT_CONFLICT)

    def test_container_folder_may_carry_the_title(self):
        ok, reason = self._verdict('Bleach - 01.mkv',
                                   container='[Judas] Bleach - Thousand-Year Blood War (Season 17)')
        self.assertTrue(ok)
        self.assertEqual(reason, IDENTITY_SEASON_TITLE)

    def test_wrong_in_season_number_is_not_rescued(self):
        ok, _ = self._verdict('Bleach - Thousand-Year Blood War - 05.mkv', target=(17, 1), numbers=(5,))
        self.assertFalse(ok)

    def test_season_one_target_never_uses_the_tier(self):
        ok, reason = episode_identity_verdict(
            target_coordinates=[(1, 1)], file_seasons=[], file_numbers=[1],
            filename='Bleach - Thousand-Year Blood War - 01.mkv', is_anime=True,
            season_titles={1: ['Thousand-Year Blood War']}, series_title='Bleach')
        self.assertTrue(ok)
        self.assertEqual(reason, IDENTITY_COORDINATE)


class FilmAndFractionalGuards(unittest.TestCase):
    def _verdict(self, filename, numbers, target=(4, 16), absolute=147, anime=True):
        return episode_identity_verdict(
            target_coordinates=[target], file_seasons=[], file_numbers=list(numbers),
            filename=filename, absolute_episode=absolute, is_anime=anime,
            series_title='Naruto')

    def test_fractional_number_is_a_special_not_the_episode(self):
        ok, reason = self._verdict(
            '[Ryuichi] Naruto - 147,5 - Las ruinas ilusorias [1080p BDREMUX].mkv', numbers=[147])
        self.assertFalse(ok)
        self.assertEqual(reason, IDENTITY_FRACTIONAL)
        ok, reason = self._verdict('Naruto - 13.5.mkv', numbers=[13], target=(1, 13), absolute=13)
        self.assertEqual(reason, IDENTITY_FRACTIONAL)

    def test_film_token_disqualifies_number_only_evidence(self):
        for name in ('Naruto - Película 2 - 147.mkv', 'Naruto Shippuden Movie 2 - 147.mkv',
                     'Naruto Gekijouban - 147.mkv', 'Naruto The Movie - 147.mkv'):
            with self.subTest(name=name):
                ok, reason = self._verdict(name, numbers=[147])
                self.assertFalse(ok)
                self.assertEqual(reason, IDENTITY_MOVIE)

    def test_plain_absolute_number_still_matches(self):
        ok, reason = self._verdict('Naruto - 147.mkv', numbers=[147])
        self.assertTrue(ok)
        self.assertEqual(reason, IDENTITY_ABSOLUTE)

    def test_size_and_codec_decimals_are_not_fractional_episodes(self):
        ok, reason = self._verdict('Naruto - 147 [2.5GB] [H.265] [DDP5.1] v1.5.mkv', numbers=[147])
        self.assertTrue(ok)
        self.assertEqual(reason, IDENTITY_ABSOLUTE)

    def test_episode_titled_movie_night_with_explicit_tag_is_fine(self):
        ok, reason = episode_identity_verdict(
            target_coordinates=[(2, 5)], file_seasons=[2], file_numbers=[5],
            filename='Show - S02E05 - The Movie Night.mkv', is_anime=True, series_title='Show')
        self.assertTrue(ok)
        self.assertEqual(reason, IDENTITY_COORDINATE)

    def test_non_anime_stated_season_ignores_the_guards(self):
        ok, reason = self._verdict('Show - Season 2 - 05 The Movie.mkv', numbers=[5],
                                   target=(2, 5), absolute=None, anime=False)
        # Non-anime with no stated season is rejected by rule 5 regardless.
        self.assertFalse(ok)
        ok, reason = episode_identity_verdict(
            target_coordinates=[(2, 5)], file_seasons=[2], file_numbers=[5],
            filename='Show - S02 - 05 The Movie.mkv', is_anime=False, series_title='Show')
        self.assertTrue(ok)
