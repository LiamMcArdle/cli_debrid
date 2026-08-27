"""Single authority for the question 'does this release belong to this season?'.

That decision used to live in three places -- the relaxed branch of
MediaMatcher._check_match, its strict branch, and filter_results.filter_results
-- as near-identical copies that drifted. Each copy offered four independent
"patterns" that could flip season_match to True, so a release only had to
satisfy the loosest of them. The observed failure: a 26-file season-1 batch of
bare-numbered files ('[SCY] Demon Slayer - 04.mkv') satisfied S04E04 and S05E04
just as readily as S01E04, because the only thing actually compared was the
episode number.

The rules here are ordered and exclusive: the first one that applies decides,
and there is no later pattern that can rescue a rejection.

Deliberately dependency-free -- no DB, no settings, no PTT. Callers supply the
already-parsed facts, which is what makes this testable in isolation.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

# Verdict reasons, for logging and tests.
EXPLICIT_MATCH = 'explicit season match'
EXPLICIT_MISMATCH = 'file names a different season'
SPECIALS = 'season 0 / specials'
CONTAINER_MATCH = 'folder names the target season'
CONTAINER_MISMATCH = 'folder names a different season'
SEASON_ONE_DEFAULT = 'no season anywhere, target is season 1'
ABSOLUTE_MATCH = 'number equals the absolute episode'
ABSOLUTE_MISMATCH = 'number is an in-season number, not the absolute'
ABSOLUTE_UNKNOWN = 'absolute episode unknown, refusing to guess for S2+'
NO_TARGET = 'no target season to check against'

def season_verdict(
    file_seasons: Optional[List[int]],
    target_season: Optional[int],
    file_numbers: Optional[List[int]],
    absolute_episode: Optional[int],
    container_season: Optional[int] = None,
    is_anime: bool = False,
    allow_bare_season_one: bool = True,
) -> Tuple[bool, str]:
    """Decide whether a file belongs to target_season.

    Args:
        file_seasons: seasons parsed from the filename itself ([] / None if it
            names none). This is the file's own claim and outranks everything.
        target_season: the season wanted. Already XEM-remapped by the caller.
        file_numbers: episode numbers parsed from the filename. For a
            bare-numbered anime release this is the ambiguous part -- the number
            may be an in-season number or an absolute one.
        absolute_episode: the absolute episode number the target S/E corresponds
            to, or None if it could not be established. None is *not* treated as
            zero-offset: see rule 5.
        container_season: season declared by the containing folder, when the
            folder pins down exactly one.
        is_anime: only anime releases get the absolute-numbering allowance.
        allow_bare_season_one: whether rule 4 applies. Strict matching passes
            False so that a file naming no season anywhere is rejected rather
            than assumed to be season 1, preserving strict mode's contract that
            the season must be stated somewhere.

    Returns:
        (matches, reason)
    """
    if target_season is None:
        return True, NO_TARGET

    seasons = [s for s in (file_seasons or []) if isinstance(s, int)]

    # 1. Specials metadata is inconsistent everywhere, so when the TARGET is a
    #    special we stay lenient about what the file calls itself.
    #
    #    The converse is NOT safe. A file that names season 0 must not satisfy a
    #    real season: allowing it picked 'Ace.of.Diamond.S00E04' (a special) as
    #    the replacement for S04E04. A season-0 claim therefore falls through to
    #    rule 2 and is rejected against any non-zero target.
    if target_season == 0:
        return True, SPECIALS

    # 2. The file names its own season. Believe it, in both directions. This is
    #    the rule that previously had four escape hatches behind it.
    #
    #    Exception: PTT reports [1] for a great many bare anime titles that
    #    state no season at all ('[Judas] Kimetsu no Yaiba - 04'), which is why
    #    the old code carried a `parsed_season_is_missing_or_default` flag
    #    everywhere. Treating that as a real claim would reject legitimate
    #    absolute-numbered releases wholesale, so for anime a lone [1] is not a
    #    claim -- it falls through to the absolute-number rule, which is strict
    #    enough to catch a genuine season-1 file aimed at a later season.
    if is_anime and seasons == [1] and target_season != 1:
        seasons = []

    if seasons:
        if target_season in seasons:
            return True, EXPLICIT_MATCH
        return False, EXPLICIT_MISMATCH

    # 3. The file is silent but its folder is not. A pack directory named
    #    'Show - S01 (1080p)' is a reliable claim about everything inside it.
    if container_season is not None:
        if container_season == target_season:
            return True, CONTAINER_MATCH
        return False, CONTAINER_MISMATCH

    # 4. Nothing names a season anywhere. Season 1 is the safe reading of a bare
    #    number, because in-season and absolute numbering coincide there.
    if target_season == 1 and allow_bare_season_one:
        return True, SEASON_ONE_DEFAULT

    # 5. Target is S2+ with a bare number. The only evidence that this file is
    #    the right season is the number being the ABSOLUTE episode. Refusing
    #    when the absolute is unknown is the point: the previous code let an
    #    empty episode-count map collapse the absolute to the plain episode
    #    number, after which every bare-numbered season-1 file matched every
    #    season of the show.
    if not is_anime:
        return False, ABSOLUTE_UNKNOWN
    if absolute_episode is None:
        return False, ABSOLUTE_UNKNOWN
    if absolute_episode in (file_numbers or []):
        return True, ABSOLUTE_MATCH
    return False, ABSOLUTE_MISMATCH


# --- Episode identity by title ---------------------------------------------
#
# Season and episode numbers are a release's *numbering*, and some packs cannot
# be reconciled with the metadata provider's numbering at all. The Danganronpa
# 'Complete Package' batch flattens four different series into one folder and
# labels every one of them S01Exx, so the name 'S01E01' belongs to four
# different episodes and nothing but the episode title separates them.
#
# This is a narrow escape hatch, not a general relaxation. A title only counts
# as evidence when it is long and specific enough to identify an episode on its
# own, AND when no other episode of the same show could claim the same filename.

TITLE_MATCH = 'filename names this episode by title'
TITLE_NOT_DISTINCTIVE = 'episode title too generic to identify an episode'
TITLE_ABSENT = 'episode title not present in filename'
TITLE_AMBIGUOUS = 'another episode of this show also fits this filename'

IDENTITY_COORDINATE = 'file names the requested coordinate'
IDENTITY_ABSOLUTE = 'file names the requested absolute episode'
IDENTITY_DATE = 'file airdate identifies the requested episode'
IDENTITY_TITLE = TITLE_MATCH
IDENTITY_EXPLICIT_CONFLICT = 'file explicitly names a conflicting coordinate'
IDENTITY_MISSING = 'file does not identify the requested episode'

MIN_TITLE_CHARS = 12
MIN_TITLE_WORDS = 2

# Placeholder titles carry no information. Providers emit these in bulk for
# shows they have no episode data for, so they would otherwise match wholesale.
_GENERIC_TITLE_RE = re.compile(
    r'^(?:episode|ep|part|chapter|act|volume|vol|special|ova|ona|movie|film|'
    r'season|series|pilot|finale|prologue|epilogue|tba|tbd|untitled|unknown)'
    r'(?:\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten))?$')


def normalize_title_text(text: str) -> str:
    """Lowercase; apostrophes deleted, every other run of non-alphanumerics a space.

    Apostrophes are deleted rather than spaced so that "Time's" and "Times"
    collapse to the same token. Spacing them would yield "time s" and stop
    "Third Time's the Charm" from matching a release that spells it "Times".
    """
    stripped = re.sub(r"['‘’ʼ`]", '', (text or '').lower())
    return re.sub(r'[^a-z0-9]+', ' ', stripped).strip()


def _strip_part_marker(title: str) -> str:
    """Drop a trailing '(2)' part marker.

    TMDB writes two-parters as 'The Gates of Warp! (1)' and 'Showdown at the
    Gates of Warp! (2)'; the release filenames carry neither marker.
    """
    return re.sub(r'\s*\(\d{1,2}\)\s*$', '', title or '').strip()


def episode_title_is_distinctive(normalized_title: str) -> bool:
    """Whether an already-normalized title is strong enough to stand as evidence."""
    if not normalized_title or len(normalized_title) < MIN_TITLE_CHARS:
        return False
    if len(normalized_title.split()) < MIN_TITLE_WORDS:
        return False
    return not _GENERIC_TITLE_RE.match(normalized_title)


def episode_title_is_usable(episode_title: str) -> bool:
    """Whether a raw episode title could identify an episode on its own."""
    return episode_title_is_distinctive(
        normalize_title_text(_strip_part_marker(episode_title)))


def episode_title_verdict(
    episode_title: Optional[str],
    filename: Optional[str],
    other_episode_titles: Optional[List[str]] = None,
    series_title: Optional[str] = None,
) -> Tuple[bool, str]:
    """Decide whether `filename` names the episode called `episode_title`.

    Args:
        episode_title: the target episode's title, as the provider gives it.
        filename: the release filename (basename only -- a pack folder may list
            many titles, which would make every file in it look like a hit).
        other_episode_titles: titles of every OTHER episode of the same show.
            This is what makes the check safe; without it a title that another
            episode also carries, or that sits inside a longer title, would be
            accepted. Callers must exclude the target episode's own title.
        series_title: the show's name, used to reject an episode titled after
            its own show.

    Returns:
        (matches, reason)
    """
    target = normalize_title_text(_strip_part_marker(episode_title))
    if not episode_title_is_distinctive(target):
        return False, TITLE_NOT_DISTINCTIVE

    # An episode named after its show ('Kingdom' in Kingdom) proves nothing:
    # every file in the show's pack contains it.
    if series_title:
        show = normalize_title_text(series_title)
        if show and target in show:
            return False, TITLE_NOT_DISTINCTIVE

    haystack = ' %s ' % normalize_title_text(filename)
    if ' %s ' % target not in haystack:
        return False, TITLE_ABSENT

    # If another episode's title also fits this filename, the filename does not
    # single ours out. Two distinct cases, both caught by comparing lengths:
    #   equal  -- two episodes genuinely share a title;
    #   longer -- containment, where 'The Gates of Warp' (E41) sits inside
    #             'Showdown at the Gates of Warp' (E42). The longer title is the
    #             one the file actually names, so E41 must not claim E42's file.
    for other in (other_episode_titles or []):
        other_norm = normalize_title_text(_strip_part_marker(other))
        if not other_norm or not episode_title_is_distinctive(other_norm):
            continue
        if len(other_norm) >= len(target) and ' %s ' % other_norm in haystack:
            return False, TITLE_AMBIGUOUS

    return True, TITLE_MATCH


_EXPLICIT_COORD_RE = re.compile(
    r'(?<![A-Za-z0-9])S(?P<season>\d{1,2})\s*[._ -]*E(?P<episode>\d{1,4})(?!\d)',
    re.IGNORECASE,
)
_EXPLICIT_X_RE = re.compile(
    r'(?<![A-Za-z0-9])(?P<season>\d{1,2})x(?P<episode>\d{1,4})(?!\d)',
    re.IGNORECASE,
)
_EXPLICIT_ANIME_SEASON_RE = re.compile(
    r'(?<![A-Za-z0-9])S(?P<season>\d{1,2})\s*[-–—]\s*'
    r'(?P<episode>\d{1,4})(?!\d)',
    re.IGNORECASE,
)

# PTT deliberately ignores a bare anime number when a release also contains a
# year (the observed ``Hunter x Hunter 1999 -81- ...`` filename parses no
# episode at all).  A number bracketed by release separators is still strong
# filename evidence, provided it is only used by the anime absolute-number
# check below.  Requiring separators on both sides avoids treating years,
# resolutions, hashes, or ordinary title digits as episodes.
_DELIMITED_ANIME_NUMBER_RE = re.compile(
    r'(?<![A-Za-z0-9])[-–—]\s*(?P<episode>\d{1,4})\s*[-–—](?![A-Za-z0-9])'
)


def delimited_anime_numbers(text: Optional[str]) -> List[int]:
    """Return bare anime numbers written as ``- 81 -`` or ``-81-``."""
    found = []
    for match in _DELIMITED_ANIME_NUMBER_RE.finditer(text or ''):
        number = int(match.group('episode'))
        if number not in found:
            found.append(number)
    return found


def explicit_coordinates(text: Optional[str]) -> List[Tuple[int, int]]:
    """Return complete S/E claims written by a filename.

    PTT frequently reports season 1 for bare anime numbers.  Reading the
    literal coordinate from the text lets identity checks distinguish that
    parser default from an actual ``S01E03`` claim.
    """
    found = []
    for pattern in (_EXPLICIT_COORD_RE, _EXPLICIT_X_RE, _EXPLICIT_ANIME_SEASON_RE):
        for match in pattern.finditer(text or ''):
            pair = (int(match.group('season')), int(match.group('episode')))
            if pair not in found:
                found.append(pair)
    return found


def episode_identity_verdict(
    *,
    target_coordinates: List[Tuple[Optional[int], Optional[int]]],
    file_seasons: Optional[List[int]],
    file_numbers: Optional[List[int]],
    filename: Optional[str] = None,
    absolute_episode: Optional[int] = None,
    container_season: Optional[int] = None,
    is_anime: bool = False,
    target_air_date: Optional[str] = None,
    file_air_date: Optional[str] = None,
    episode_title: Optional[str] = None,
    other_episode_titles: Optional[List[str]] = None,
    series_title: Optional[str] = None,
    allow_bare_season_one: bool = True,
) -> Tuple[bool, str]:
    """Decide whether one file identifies one requested episode.

    Coordinate alternatives are evaluated as pairs.  This deliberately does
    not offer separate "original season" and "original episode" fallbacks;
    doing so admitted Cartesian combinations such as mapped S02 + stored E25.
    Explicit coordinates in the filename outrank all softer evidence.
    """
    coordinates = []
    for season, episode in target_coordinates or []:
        try:
            pair = (int(season) if season is not None else None,
                    int(episode) if episode is not None else None)
        except (TypeError, ValueError):
            continue
        if pair[1] is not None and pair not in coordinates:
            coordinates.append(pair)
    if not coordinates:
        return False, IDENTITY_MISSING

    explicit = explicit_coordinates(filename)
    if explicit:
        if any(pair in coordinates for pair in explicit):
            return True, IDENTITY_COORDINATE
        # Anime pack filenames commonly encode an absolute episode as
        # S01E112 even when metadata stores it in a later logical season.
        # This is still exact evidence, but only when BOTH the conventional
        # season-one marker and the known absolute number agree.
        if is_anime and absolute_episode is not None \
                and (1, int(absolute_episode)) in explicit:
            return True, IDENTITY_ABSOLUTE
        return False, IDENTITY_EXPLICIT_CONFLICT

    numbers = [n for n in (file_numbers or []) if isinstance(n, int)]
    if is_anime:
        for number in delimited_anime_numbers(filename):
            if number not in numbers:
                numbers.append(number)
    seasons = [s for s in (file_seasons or []) if isinstance(s, int)]

    for target_season, target_episode in coordinates:
        season_ok, _ = season_verdict(
            file_seasons=seasons,
            target_season=target_season,
            file_numbers=numbers,
            absolute_episode=absolute_episode,
            container_season=container_season,
            is_anime=is_anime,
            allow_bare_season_one=allow_bare_season_one,
        )
        if season_ok and target_episode in numbers:
            return True, IDENTITY_COORDINATE

    # Absolute numbering is one show-global identity, independent of which
    # legitimate S/E coordinate was used to reach the result.
    if is_anime and absolute_episode is not None and absolute_episode in numbers:
        return True, IDENTITY_ABSOLUTE

    if target_air_date and file_air_date and target_air_date == file_air_date:
        return True, IDENTITY_DATE

    title_ok, title_reason = episode_title_verdict(
        episode_title,
        filename,
        other_episode_titles=other_episode_titles,
        series_title=series_title,
    )
    if title_ok:
        return True, IDENTITY_TITLE
    return False, title_reason if episode_title else IDENTITY_MISSING


def container_season_from_path(file_path: str) -> Optional[int]:
    """Season the containing folder declares, only when the claim is unambiguous.

    Returns None whenever the folder cannot pin down exactly one season, so
    multi-season packs fall through to the absolute-number rule rather than
    being mislabelled as their first season.
    """
    # Lookarounds rather than consuming groups: 'S01+S02' must yield BOTH
    # seasons, and a pattern that ate the '+' as S01's trailing delimiter would
    # leave nothing for S02 to match, reporting a two-season pack as season 1.
    season_re = re.compile(r'(?<![A-Za-z0-9])S(\d{1,2})(?![0-9])', re.I)
    word_re = re.compile(r'(?<![A-Za-z0-9])Season\s*(\d{1,2})(?![0-9])', re.I)
    span_re = re.compile(
        r'(?<![A-Za-z0-9])(?:S|Season\s*)\d{1,2}\s*[-+&,~]\s*(?:S|Season\s*)?\d{1,2}', re.I)
    multi = ('complete', 'seasons', 'batch', ' to ', 'collection', 'colection')

    folder = os.path.basename(os.path.dirname(file_path or ''))
    if not folder:
        return None
    if any(w in folder.lower() for w in multi):
        return None
    if span_re.search(folder):
        return None
    hits = {int(m) for m in season_re.findall(folder)}
    hits |= {int(m) for m in word_re.findall(folder)}
    return hits.pop() if len(hits) == 1 else None


def log_verdict(matches: bool, reason: str, filename: str,
                target_season: Optional[int], target_episode: Optional[int]) -> None:
    """Log at info when a release is rejected, debug when accepted.

    Rejections are the interesting event when diagnosing 'why did nothing get
    collected', and there are far fewer of them than acceptances.
    """
    msg = (f"Season check {'passed' if matches else 'REJECTED'} "
           f"for S{target_season}E{target_episode}: {reason} -- '{filename}'")
    if matches:
        logging.debug(msg)
    else:
        logging.info(msg)
