"""has_non_latin_letter must look at every letter, not just find one Latin one.

The regex it replaces, [A-Za-z], classified '헌터X헌터' as Latin on the
strength of its single 'X' and sent it to every indexer. 21% of alias queries
in one measured hour were in scripts no indexer answers in.
"""

import unittest

from utilities.text_script import has_non_latin_letter


class NonLatinDetection(unittest.TestCase):
    def test_plain_latin(self):
        self.assertFalse(has_non_latin_letter('Hunter x Hunter'))

    def test_accented_latin_is_latin(self):
        self.assertFalse(has_non_latin_letter('Pokémon'))
        self.assertFalse(has_non_latin_letter('Señor de los Anillos'))

    def test_fullwidth_latin_normalises_to_latin(self):
        self.assertFalse(has_non_latin_letter('ＢＬＥＡＣＨ'))

    def test_one_latin_letter_does_not_make_hangul_latin(self):
        self.assertTrue(has_non_latin_letter('헌터X헌터'))

    def test_japanese(self):
        self.assertTrue(has_non_latin_letter('ジョジョの奇妙な冒険'))
        self.assertTrue(has_non_latin_letter('BLEACH 千年血戦篇'))

    def test_hebrew_cyrillic_arabic_thai_greek(self):
        for text in ('האנטר X האנטר', 'Невероятные приключения ДжоДжо',
                     'هانتر x هانتر', 'ฮันเตอร์ x ฮันเตอร์', 'Χάντερ'):
            with self.subTest(text=text):
                self.assertTrue(has_non_latin_letter(text))

    def test_digits_and_punctuation_are_not_letters(self):
        self.assertFalse(has_non_latin_letter('86 - Eighty Six (2021)'))
        self.assertFalse(has_non_latin_letter('★ 2.5 ★'))

    def test_empty_and_none(self):
        self.assertFalse(has_non_latin_letter(''))
        self.assertFalse(has_non_latin_letter(None))


if __name__ == '__main__':
    unittest.main()
