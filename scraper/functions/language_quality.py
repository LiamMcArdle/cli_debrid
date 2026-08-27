"""Small, dependency-free release-name accessibility signals."""

import re
from typing import Iterable, Tuple


_ENGLISH_SUBS = re.compile(
    r'(?<![a-z])(?:eng|english)[ ._-]*(?:sub|subs|subtitle|subtitles)(?![a-z])', re.I)
_MULTI_SUBS = re.compile(
    r'(?<![a-z])(?:multi[ ._-]*subs?|multisubs?)(?![a-z])', re.I)
_ENGLISH_AUDIO = re.compile(
    r'(?<![a-z])(?:eng|english)[ ._-]*(?:audio|dub|dubbed)(?![a-z])|'
    r'(?<![a-z])(?:dual[ ._-]*audio)(?![a-z])', re.I)
_NON_ENGLISH_ONLY = re.compile(
    r'(?<![a-z])vostfr(?![a-z])|'
    r'(?<![a-z])(?:french|rus|russian|spa|spanish|latino|german|ger|'
    r'italian|ita|portuguese|por|pt[ ._-]*br)[ ._-]*(?:sub|subs|dub|dubbed)(?![a-z])',
    re.I,
)


def english_accessibility_score(texts: Iterable[str]) -> Tuple[int, str]:
    """Score explicit English accessibility; absence is deliberately neutral."""
    text = ' '.join(str(value) for value in (texts or []) if value)
    if not text:
        return 0, 'no accessibility evidence'
    if _ENGLISH_SUBS.search(text):
        return 50, 'explicit English subtitles'
    if _MULTI_SUBS.search(text):
        return 40, 'explicit multi-subs'
    if _ENGLISH_AUDIO.search(text):
        return 35, 'explicit English or dual audio'
    if _NON_ENGLISH_ONLY.search(text):
        return -35, 'explicit non-English-only subtitles/audio'
    return 0, 'unknown accessibility'
