import importlib.util
import os
import unittest


_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'scraper', 'functions', 'language_quality.py')
_spec = importlib.util.spec_from_file_location('language_quality', _PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


class TestEnglishAccessibilityScore(unittest.TestCase):
    def test_unknown_is_neutral(self):
        self.assertEqual(_module.english_accessibility_score(
            ['Hunter x Hunter 1999 720p'])[0], 0)

    def test_english_subs_beat_unknown(self):
        self.assertGreater(_module.english_accessibility_score(
            ['Hunter x Hunter ENG SUB'])[0], 0)

    def test_multi_subs_are_positive(self):
        self.assertGreater(_module.english_accessibility_score(
            ['Hunter x Hunter [Multi Subs]'])[0], 0)

    def test_explicit_non_english_only_is_penalized(self):
        self.assertLess(_module.english_accessibility_score(
            ['Hunter x Hunter VOSTFR'])[0], 0)

    def test_english_evidence_wins_over_non_english_tag(self):
        self.assertGreater(_module.english_accessibility_score(
            ['Hunter x Hunter English Subs French Subs'])[0], 0)


if __name__ == '__main__':
    unittest.main()
