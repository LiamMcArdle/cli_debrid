"""Script classification for titles and aliases.

Release groups title their files in Latin script, or in a show's own native
script on Nyaa. Nothing is published under a Korean, Hebrew, Cyrillic or Thai
name, so those aliases are worth matching against but never worth querying
with. The test has to look at every letter: a single-character regex such as
``[A-Za-z]`` classified '헌터X헌터' as Latin on the strength of its one 'X'.
"""

import unicodedata


def has_non_latin_letter(text) -> bool:
    """True if any letter in ``text`` is from a non-Latin script.

    Accented Latin ('Pokémon'), fullwidth Latin ('ＢＬＥＡＣＨ') and digits or
    punctuation from any script count as Latin. Kana, kanji, Hangul, Cyrillic,
    Hebrew, Arabic, Thai and Greek letters do not.
    """
    if not text:
        return False
    for char in unicodedata.normalize('NFKC', str(text)):
        if not unicodedata.category(char).startswith('L'):
            continue
        if not unicodedata.name(char, '').startswith('LATIN'):
            return True
    return False
