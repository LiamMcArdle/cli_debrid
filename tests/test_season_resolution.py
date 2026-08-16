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

    def test_specials_are_exempt(self):
        ok, reason = season_verdict(
            file_seasons=[0], target_season=3, file_numbers=[2],
            absolute_episode=35, is_anime=True)
        self.assertTrue(ok)
        self.assertEqual(reason, SPECIALS)


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
