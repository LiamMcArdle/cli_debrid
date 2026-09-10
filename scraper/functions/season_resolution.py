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
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

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


def absolute_candidates(absolute_episode: Any) -> Tuple[int, ...]:
    """Normalise the absolute-episode argument to distinct candidate numbers.

    A caller may know more than one plausible absolute number for the same
    target: the scene/XEM number stamped on the result, and the number derived
    from the metadata provider's own season lengths. These disagree whenever
    the provider's season boundaries differ from the scene's, and neither is
    authoritative, so a caller may pass both and any one of them matching is
    proof. An int (or None) is still accepted unchanged, so every existing
    caller keeps its meaning.
    """
    if absolute_episode is None:
        return ()
    if isinstance(absolute_episode, bool):
        return ()
    if isinstance(absolute_episode, int):
        return (absolute_episode,)
    candidates: List[int] = []
    for value in absolute_episode:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number not in candidates:
            candidates.append(number)
    return tuple(candidates)


def season_verdict(
    file_seasons: Optional[List[int]],
    target_season: Optional[int],
    file_numbers: Optional[List[int]],
    absolute_episode: Union[int, Sequence[int], None],
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
            zero-offset: see rule 5. A sequence may be passed when more than one
            numbering is plausible (scene/XEM and provider-derived); any one of
            them matching is proof.
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
    absolutes = absolute_candidates(absolute_episode)
    if not absolutes:
        return False, ABSOLUTE_UNKNOWN
    numbers = file_numbers or []
    if any(candidate in numbers for candidate in absolutes):
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
IDENTITY_SEASON_TITLE = 'file names the requested season by its title'
IDENTITY_SEASON_TITLE_CONFLICT = 'file names a different season by its title'
IDENTITY_DATE = 'file airdate identifies the requested episode'
IDENTITY_TITLE = TITLE_MATCH
IDENTITY_EXPLICIT_CONFLICT = 'file explicitly names a conflicting coordinate'
IDENTITY_BEYOND_SERIES = 'episode range exceeds the known series extent'
IDENTITY_MOVIE = 'file names a film, its number is not an episode'
IDENTITY_FRACTIONAL = 'file carries a fractional episode number (a special), not this episode'
IDENTITY_MISSING = 'file does not identify the requested episode'

# Verdicts that mean "could not prove this is the episode", as opposed to
# "proved this is a different episode". Only the second kind is evidence of
# wrongness and may reject a release on its own.
#
# The distinction is load bearing. Numbered anime releases carry no episode
# title, so once the absolute-number rule cannot answer -- an unknown absolute,
# or a provider whose season boundaries disagree with the scene's -- the title
# fallback is guaranteed to come back TITLE_ABSENT. Treating that silence as a
# rejection made the gate discard every candidate for a whole show, which then
# exhausted the retry ladder and was blacklisted. An inconclusive verdict must
# hand the decision back to the season/episode checks that ran before this gate
# existed, not pre-empt them.
INCONCLUSIVE_IDENTITY_REASONS = frozenset({
    TITLE_ABSENT,
    TITLE_NOT_DISTINCTIVE,
    IDENTITY_MISSING,
})

# A number is only evidence of an episode when the file is an episode. Fansub
# groups number films and specials as '147,5' / '13.5' and PTT reads the
# integer part; a 28.9 GB Spanish BD remux of Naruto Movie 2 filled S04E16 that
# way. Both guards apply only where the NUMBER is the whole case -- the anime
# bare-number and absolute-number paths -- never where an explicit coordinate,
# an airdate or an episode title identifies the file.
_MOVIE_TOKEN_RE = re.compile(
    r'(?<![a-z])(?:the movie(?!\s*[a-z])|movie\s*\d+|film\b|pel[ií]cula\b|gekijou-?ban\b|劇場版)',
    re.IGNORECASE,
)
_FRACTIONAL_EPISODE_RE = re.compile(r'(?<![A-Za-z0-9])(?P<episode>\d{1,4})[.,]5(?!\d)(?!\s*(?:gb|mb|gib|mib|mbps|kbps)\b)',
                                    re.IGNORECASE)


def _film_token(text: Optional[str]) -> bool:
    return bool(text) and _MOVIE_TOKEN_RE.search(text) is not None


def _fractional_numbers(text: Optional[str]) -> List[int]:
    """Integer parts of every 'N,5' / 'N.5' in the text."""
    return [int(m.group('episode')) for m in _FRACTIONAL_EPISODE_RE.finditer(text or '')]

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


def _names_within(target: str, haystack: str, loose_spacing: bool) -> bool:
    """Whether the normalized ``target`` occurs in the normalized ``haystack``.

    With ``loose_spacing`` the comparison also ignores word breaks, so a
    provider's 'Fishman Island' finds a release's 'Fish-Man Island Saga'.
    Only for names long enough that a run of letters cannot appear by
    accident inside other words.
    """
    if ' %s ' % target in ' %s ' % haystack:
        return True
    if loose_spacing:
        packed = target.replace(' ', '')
        if len(packed) >= 8 and packed in haystack.replace(' ', ''):
            return True
    return False


def episode_title_verdict(
    episode_title: Optional[str],
    filename: Optional[str],
    other_episode_titles: Optional[List[str]] = None,
    series_title: Optional[str] = None,
    loose_spacing: bool = False,
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

    haystack = normalize_title_text(filename)
    if not _names_within(target, haystack, loose_spacing):
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
        if len(other_norm) >= len(target) and _names_within(other_norm, haystack, loose_spacing):
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
    # The trailing guards refuse the things that follow a season marker but
    # are not an episode number: a resolution ('S01 - 1080p'), a bit depth or
    # frame rate ('S01 - 10bit', 'S01 - 60fps'), a channel layout ('S01 - 5.1')
    # or a decimal of any kind. Without them "Show - S01 - 1080p" read as the
    # explicit coordinate S01E1080 and the conflict short-circuit rejected every
    # correctly numbered file in the pack. Years are refused in
    # explicit_coordinates, where the value is known.
    r'(?<![A-Za-z0-9])S(?P<season>\d{1,2})\s*[-–—]\s*'
    r'(?P<episode>\d{1,4})(?!\d)(?![.,]\d)(?![pik])(?!\s*(?:bit|fps|hz)\b)',
    re.IGNORECASE,
)

# Continuations of an explicit coordinate: the extra episodes named by
# multi-episode files such as S01E01E02, S01E01-E02, S01E01-02 or 1x01x02.
# Without reading them, the first episode is the only coordinate the file is
# credited with and every later episode of the span is rejected as an
# explicit conflict.  The x-branch refuses codec tokens (x264/x265/x266) and
# the trailing guard refuses resolution suffixes (-1080p).
_EXPLICIT_SPAN_CONT_RE = re.compile(
    r'(?:[._ -]{0,3}E|[._ -]{0,3}x(?!26[456])|-)(?P<episode>\d{1,4})(?![\dpi])',
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
    src = text or ''
    for pattern in (_EXPLICIT_COORD_RE, _EXPLICIT_X_RE, _EXPLICIT_ANIME_SEASON_RE):
        for match in pattern.finditer(src):
            season = int(match.group('season'))
            first = int(match.group('episode'))
            # 'Show - S01 - 2019 - 03' names a year after the season marker,
            # not episode 2019. One Piece really does reach S21 - 1000, so only
            # the four-digit year band is refused.
            if pattern is _EXPLICIT_ANIME_SEASON_RE and 1900 <= first <= 2099 \
                    and len(match.group('episode')) == 4:
                continue
            episodes = [first]
            # Read multi-episode continuations (S01E01E02, S01E01-E02, ...)
            # so every episode of the span counts as explicitly named.
            pos = match.end()
            while True:
                cont = _EXPLICIT_SPAN_CONT_RE.match(src, pos)
                if not cont:
                    break
                episodes.append(int(cont.group('episode')))
                pos = cont.end()
            # A two-number ascending span is a range (S01E01-E13 names all
            # thirteen episodes).  The cap keeps a stray year or hash from
            # fabricating hundreds of coordinates.
            if (len(episodes) == 2 and episodes[0] < episodes[1]
                    and episodes[1] - episodes[0] <= 100):
                episodes = list(range(episodes[0], episodes[1] + 1))
            for episode in episodes:
                pair = (season, episode)
                if pair not in found:
                    found.append(pair)
    return found


def episode_identity_verdict(
    *,
    target_coordinates: List[Tuple[Optional[int], Optional[int]]],
    file_seasons: Optional[List[int]],
    file_numbers: Optional[List[int]],
    filename: Optional[str] = None,
    absolute_episode: Union[int, Sequence[int], None] = None,
    max_absolute_episode: Optional[int] = None,
    container_season: Optional[int] = None,
    is_anime: bool = False,
    target_air_date: Optional[str] = None,
    file_air_date: Optional[str] = None,
    episode_title: Optional[str] = None,
    other_episode_titles: Optional[List[str]] = None,
    series_title: Optional[str] = None,
    allow_bare_season_one: bool = True,
    season_titles: Optional[Dict[Any, List[str]]] = None,
    container_text: Optional[str] = None,
) -> Tuple[bool, str]:
    """Decide whether one file identifies one requested episode.

    Coordinate alternatives are evaluated as pairs.  This deliberately does
    not offer separate "original season" and "original episode" fallbacks;
    doing so admitted Cartesian combinations such as mapped S02 + stored E25.
    Explicit coordinates in the filename outrank all softer evidence.

    ``season_titles`` maps season number -> the provider's names for that
    season ('Thousand-Year Blood War'). Sequel seasons are released under
    their arc name with numbering restarted at 01; nothing carries the
    absolute number rule 5 demands, so without this tier they were
    unobtainable. A distinctive season title in the filename (or in
    ``container_text``, the pack folder or release title) is an explicit
    season claim for that season and lets the in-season number decide. A file
    naming no season title is unaffected, so a season-1 release still cannot
    be promoted.
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
        if is_anime and any((1, candidate) in explicit
                            for candidate in absolute_candidates(absolute_episode)):
            return True, IDENTITY_ABSOLUTE
        return False, IDENTITY_EXPLICIT_CONFLICT

    numbers = [n for n in (file_numbers or []) if isinstance(n, int)]
    if is_anime:
        for number in delimited_anime_numbers(filename):
            if number not in numbers:
                numbers.append(number)
    seasons = [s for s in (file_seasons or []) if isinstance(s, int)]

    # Number-only evidence is disqualified when the file says it is a film or
    # carries a fractional number. Checked here, after explicit coordinates,
    # so an episode genuinely titled 'Movie Night' with an S02E05 tag is safe.
    film = _film_token(filename) or _film_token(container_text)
    fractional = set(_fractional_numbers(filename))

    def _number_blocked(number):
        if film:
            return IDENTITY_MOVIE
        if number in fractional:
            return IDENTITY_FRACTIONAL
        return None

    # Season-title tier: a distinctive season name in the file or its
    # container is an explicit claim for that season.
    if season_titles:
        by_season = {}
        for key, names in season_titles.items():
            try:
                by_season[int(key)] = [n for n in (names or []) if n]
            except (TypeError, ValueError):
                continue
        def _claimed(name, others):
            for text in (filename, container_text):
                if not text:
                    continue
                ok, _ = episode_title_verdict(name, text, other_episode_titles=others,
                                              series_title=series_title, loose_spacing=True)
                if ok:
                    return True
            return False

        for target_season, target_episode in coordinates:
            if target_season is None:
                continue
            names = by_season.get(target_season) or []
            others = [n for s, ns in by_season.items() if s != target_season for n in ns]
            if target_season >= 2 and names:
                for name in names:
                    if _claimed(name, others) and target_episode in numbers:
                        blocked = _number_blocked(target_episode) if is_anime else None
                        if blocked:
                            return False, blocked
                        return True, IDENTITY_SEASON_TITLE
            # A file that names ANOTHER season's arc is a claim for that season.
            # 'One Piece Log - Fish-Man Island Saga - 13' is episode 13 of the
            # Fish-Man Island season; for S20E13 the bare 13 matched and the
            # file was grabbed, then refused at the add. An explicit arc name
            # is a coordinate and conflicts like one.
            for other_season, other_names in by_season.items():
                if other_season == target_season:
                    continue
                rest = [n for s, ns in by_season.items() if s != other_season for n in ns]
                for other_name in other_names:
                    if _claimed(other_name, rest):
                        return False, IDENTITY_SEASON_TITLE_CONFLICT

    # A complete/batch range can contain both the requested in-season number
    # and its absolute number while still belonging to a different adaptation.
    # Hunter x Hunter (2011) batches advertise 01-148, which used to satisfy
    # HxH (1999) S04E03 because that list contains both 3 and 81.  Metadata's
    # show-global absolute maximum distinguishes them: the 1999 series ends at
    # 92.  Apply this only to multi-number results; a single selected file is
    # already checked against the exact target below.
    if is_anime and max_absolute_episode is not None and len(set(numbers)) > 1:
        try:
            if max(numbers) > int(max_absolute_episode):
                return False, IDENTITY_BEYOND_SERIES
        except (TypeError, ValueError):
            pass

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
            # For anime this is a bare (or PTT-guessed) number; the film and
            # fractional guards apply. Non-anime reached here through a stated
            # season, where a number is a number.
            blocked = _number_blocked(target_episode) if is_anime else None
            if blocked:
                return False, blocked
            return True, IDENTITY_COORDINATE

    # Absolute numbering is one show-global identity, independent of which
    # legitimate S/E coordinate was used to reach the result.
    if is_anime:
        for candidate in absolute_candidates(absolute_episode):
            if candidate not in numbers:
                continue
            blocked = _number_blocked(candidate)
            if blocked:
                return False, blocked
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
    word_re = re.compile(r'(?<![A-Za-z0-9])Seasons?\s*(\d{1,2})(?![0-9])', re.I)
    # A span names more than one season however it is written: 'S01-S03',
    # 'Season 1 to 3', 'Seasons 1-5'.
    span_re = re.compile(
        r'(?<![A-Za-z0-9])(?:S|Seasons?\s*)\d{1,2}\s*(?:[-+&,~]|to)\s*(?:S|Season\s*)?\d{1,2}', re.I)

    folder = os.path.basename(os.path.dirname(file_path or ''))
    if not folder:
        return None
    # Only a span or two different season numbers make the folder ambiguous.
    # Words like 'batch' or 'complete' used to disqualify the folder outright,
    # which sent 'Show S02 [Batch]/Show - 03.mkv' to the absolute-number rule
    # and rejected every per-season bare-numbered file for S2+.
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


# ---------------------------------------------------------------------------
# Absolute episode number by identity
# ---------------------------------------------------------------------------
#
# The metadata battery and media_items hold different season layouts for the
# same show: Trakt re-splits a long-running series, an episode moves to
# season 0, a dual numbering tree is merged, seasons are reordered. Reading
# the battery's absolute_episode by positional (season, episode) then lands on
# a different real episode -- Dragon Ball Kai S2E1 read 99 where the episode
# the library row names is 27 -- and every search goes out with a number no
# release carries. The episode's own title and air date identify it across
# layouts; these functions resolve the absolute number from that evidence.
#
# Title evidence decides; date evidence only confirms. Measured over the 9,493
# pending anime rows on 2026-09-09, a draft that verified the positional row
# first returned the wrong tree for 391 rows (the same title also sat at the
# number the releases carry), and a draft that remapped by date moved 63 rows
# onto their neighbours because media_items' dates were stale.

from datetime import date as _date, datetime as _datetime

ABS_VERIFIED_TITLE = 'ABS_VERIFIED_TITLE'
ABS_REMAPPED_TITLE = 'ABS_REMAPPED_TITLE'
ABS_AMBIGUOUS_DATE = 'ABS_AMBIGUOUS_DATE'
ABS_AMBIGUOUS_ESTIMATE = 'ABS_AMBIGUOUS_ESTIMATE'
ABS_AMBIGUOUS = 'ABS_AMBIGUOUS'
ABS_VERIFIED_DATE = 'ABS_VERIFIED_DATE'
ABS_POSITIONAL_UNCONFIRMED = 'ABS_POSITIONAL_UNCONFIRMED'
ABS_REMAPPED_DATE = 'ABS_REMAPPED_DATE'
ABS_UNVERIFIED = 'ABS_UNVERIFIED'
ABS_UNRESOLVED = 'ABS_UNRESOLVED'
ABS_NO_ROWS = 'ABS_NO_ROWS'

_LEADING_DATE_RE = re.compile(r'^\s*(\d{4})-(\d{2})-(\d{2})')


def _leading_date(text: str) -> Optional[_date]:
    match = _LEADING_DATE_RE.match(text or '')
    if not match:
        return None
    try:
        return _date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def parse_local_date(value: Any) -> Optional[_date]:
    """A media_items release_date as a calendar day.

    Accepts date/datetime objects and 'YYYY-MM-DD' with an optional time part.
    Rejects None, '' and the 'Unknown' placeholder that metadata writes when a
    provider has no date.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, _datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    text = str(value).strip()
    if not text or text.lower() in ('unknown', 'none', 'null'):
        return None
    return _leading_date(text)


def parse_utc_date(value: Any) -> Optional[_date]:
    """A battery first_aired (a UTC instant) as the UTC calendar day.

    No timezone conversion: the caller compares with a one-day tolerance that
    absorbs the offset between a UTC instant and a local calendar day.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, _datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    return _leading_date(str(value).strip().rstrip('Zz'))


def dates_within_a_day(a: Optional[_date], b: Optional[_date]) -> bool:
    """Same day or adjacent days. Weekly episodes are seven apart, so this
    cannot bridge two episodes; it only absorbs the UTC/local offset."""
    if a is None or b is None:
        return False
    return abs((a - b).days) <= 1


def episode_title_key(title: Any, strip_part: bool = False) -> str:
    """Comparison key for an episode title.

    Exact by default. Two-parters differ only by a trailing '(1)' / '(2)',
    so stripping the marker up front makes every one of them ambiguous; the
    stripped form is a fallback for when the exact key finds nothing.
    """
    text = title if isinstance(title, str) else ('' if title is None else str(title))
    if strip_part:
        text = _strip_part_marker(text)
    return normalize_title_text(text)


def episode_title_is_specific(title: Any) -> bool:
    """Whether a title can identify an episode among the show's own rows.

    Looser than episode_title_is_distinctive on purpose: that guard protects a
    substring search inside filenames; here the comparison is exact equality
    against the show's own episode list with uniqueness enforced, so a short
    title is fine and only placeholders ('Episode 18') are excluded.
    """
    key = episode_title_key(title)
    return bool(key) and not _GENERIC_TITLE_RE.match(key)


def _row_field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def absolute_episode_from_identity(
    rows: Sequence[Any],
    season: int,
    episode: int,
    episode_title: Any,
    release_date: Any,
    estimate: Optional[int] = None,
) -> Tuple[Optional[int], str, Tuple[int, ...]]:
    """Resolve the absolute number of S{season}E{episode} from the show's rows.

    ``rows`` are the battery's episodes for the show, each exposing
    season_number, episode_number, title, first_aired and absolute_episode
    (attributes or dict keys). ``episode_title`` and ``release_date`` are the
    library row's own identity. ``estimate`` is the arithmetic number derived
    from the library's season layout; it is the weakest evidence and only ever
    breaks a tie.

    Returns (absolute, verdict, candidates). ``candidates`` is non-empty only
    for the ambiguous verdicts and lists every absolute the title sits at; for
    ABS_AMBIGUOUS the caller should ask for every one of them. ``absolute`` is
    None only for ABS_UNRESOLVED, where the caller falls back to its other
    sources.

    Order of evidence:
      1. The title, searched over every non-special row. One absolute:
         verified (equals the positional) or remapped. Several: the one whose
         air date matches, else the estimate if it is among them, else
         ambiguous with every candidate returned. The positional row never
         short-circuits this search -- on dual numbering trees the positional
         title matches and the number releases carry is the OTHER one.
      2. No title hit and a positional row with an absolute: kept, verified by
         date when the dates agree, otherwise unconfirmed. A stale library
         date is not evidence of a different episode.
      3. No positional absolute: a single row on the library's date, else
         unresolved.
    """
    positional = None
    for row in rows or ():
        if _row_field(row, 'season_number') == season and _row_field(row, 'episode_number') == episode:
            positional = row
            break
    positional_abs = _row_field(positional, 'absolute_episode') if positional is not None else None
    if positional_abs is not None:
        try:
            positional_abs = int(positional_abs)
        except (TypeError, ValueError):
            positional_abs = None

    candidates = []
    for row in rows or ():
        absolute = _row_field(row, 'absolute_episode')
        if absolute is None:
            continue
        row_season = _row_field(row, 'season_number')
        if not (isinstance(row_season, int) and row_season > 0) and season != 0:
            # Specials reuse titles (recaps) and manufacture ambiguity.
            continue
        try:
            candidates.append((int(absolute), row))
        except (TypeError, ValueError):
            continue
    if not candidates:
        return positional_abs, ABS_NO_ROWS, ()

    media_date = parse_local_date(release_date)
    title_specific = episode_title_is_specific(episode_title)
    if not title_specific and media_date is None:
        return positional_abs, ABS_UNVERIFIED, ()

    if title_specific:
        exact_key = episode_title_key(episode_title)
        hits = [(absolute, row) for absolute, row in candidates
                if episode_title_key(_row_field(row, 'title')) == exact_key]
        if not hits:
            stripped_key = episode_title_key(episode_title, strip_part=True)
            hits = [(absolute, row) for absolute, row in candidates
                    if episode_title_key(_row_field(row, 'title'), strip_part=True) == stripped_key]
        found = tuple(sorted({absolute for absolute, _ in hits}))
        if len(found) == 1:
            verdict = ABS_VERIFIED_TITLE if found[0] == positional_abs else ABS_REMAPPED_TITLE
            return found[0], verdict, ()
        if len(found) > 1:
            by_date = tuple(sorted({
                absolute for absolute, row in hits
                if dates_within_a_day(media_date, parse_utc_date(_row_field(row, 'first_aired')))
            }))
            if len(by_date) == 1:
                return by_date[0], ABS_AMBIGUOUS_DATE, found
            if estimate is not None and estimate in found:
                return int(estimate), ABS_AMBIGUOUS_ESTIMATE, found
            # Every candidate is asked for; the primary is the one the
            # positional row already names when it is among them (a
            # two-parter with identical titles), else the lowest.
            primary = positional_abs if positional_abs in found else found[0]
            return primary, ABS_AMBIGUOUS, found

    if positional_abs is not None:
        positional_date = parse_utc_date(_row_field(positional, 'first_aired'))
        if dates_within_a_day(media_date, positional_date):
            return positional_abs, ABS_VERIFIED_DATE, ()
        return positional_abs, ABS_POSITIONAL_UNCONFIRMED, ()

    if media_date is not None:
        on_date = tuple(sorted({
            absolute for absolute, row in candidates
            if dates_within_a_day(media_date, parse_utc_date(_row_field(row, 'first_aired')))
        }))
        if len(on_date) == 1:
            return on_date[0], ABS_REMAPPED_DATE, ()
    return None, ABS_UNRESOLVED, ()
