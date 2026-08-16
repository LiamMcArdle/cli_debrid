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

    # 1. Specials are bundled into ordinary season packs all the time, so a
    #    season-0 claim never counts as a conflict.
    if 0 in seasons or target_season == 0:
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


def container_season_from_path(file_path: str) -> Optional[int]:
    """Season the containing folder declares, only when the claim is unambiguous.

    Returns None whenever the folder cannot pin down exactly one season, so
    multi-season packs fall through to the absolute-number rule rather than
    being mislabelled as their first season.
    """
    import os
    import re

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
