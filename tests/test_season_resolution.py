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


if __name__ == '__main__':
    unittest.main(verbosity=2)
