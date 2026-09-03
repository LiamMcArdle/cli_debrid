"""
Media matching module for handling content validation and file matching logic.
Separates the media matching concerns from queue management.
"""

import logging
import os
import re
from typing import Dict, Any, List, Tuple, Optional
from collections import defaultdict
from fuzzywuzzy import fuzz
from PTT import parse_title
from scraper.functions.anime_utils import detect_absolute_numbering
from scraper.functions.season_resolution import (
    season_verdict, container_season_from_path, log_verdict,
    episode_title_verdict, episode_title_is_usable,
    episode_identity_verdict)
from scraper.functions.similarity_checks import (
    title_verdict, title_is_asserted, MIN_TITLE_MATCH)

# Words by which a release states it IS a special rather than a numbered
# episode. Used only for season 0, where a bare number is not evidence.
_SPECIAL_ASSERT_RE = re.compile(
    r'\b(ova|oad|ona|special|specials|sp\d{1,2}|movie|film|gekijou-?ban|recap|'
    r'picture[ ._-]?drama|omake|extra|bonus|blooper|preview|pv|cm|'
    r'short|shorts|mini|episode[ ._-]?0)\b', re.IGNORECASE)

# An explicit season-zero coordinate written by the release itself.
_SEASON_ZERO_COORD_RE = re.compile(r'\bS00E\d{1,3}\b|\b0x\d{1,3}\b', re.IGNORECASE)

# Titles that name a film rather than an episode. Gekijouban is the Japanese
# equivalent and appears on anime film releases that sit in series packs.
_MOVIE_TITLE_RE = re.compile(r'\b(the movie|movie\s*\d+|gekijouban|gekijou-ban)\b', re.IGNORECASE)


class MediaMatcher:
    """Handles media content matching and validation"""

    def __init__(self, relaxed_matching: bool = False):
        self.episode_count_cache: Dict[str, Dict[int, int]] = {}
        self.episode_title_cache: Dict[str, Dict[Tuple[int, int], str]] = {}
        self.official_titles_cache: Dict[str, Optional[List[str]]] = {}
        self.relaxed_matching = relaxed_matching

    def _get_episode_titles_cached(self, imdb_id: Optional[str]) -> Dict[Tuple[int, int], str]:
        """(season, episode) -> episode title for every episode of one show.

        Exists solely to prove an episode title is unique within its show, which
        is the guard that makes title matching safe. Returns {} on any failure;
        the caller treats an empty map as "cannot verify" and declines rather
        than falling back to the title alone.
        """
        if not imdb_id:
            return {}
        if imdb_id in self.episode_title_cache:
            return self.episode_title_cache[imdb_id]
        titles: Dict[Tuple[int, int], str] = {}
        try:
            from database.core import get_db_connection
            conn = get_db_connection()
            try:
                for season, episode, title in conn.execute(
                        "SELECT DISTINCT season_number, episode_number, episode_title "
                        "FROM media_items WHERE imdb_id = ? AND type = 'episode' "
                        "AND episode_title IS NOT NULL AND episode_title != ''",
                        (imdb_id,)):
                    if season is not None and episode is not None:
                        titles[(season, episode)] = title
            finally:
                conn.close()
        except Exception as e:
            logging.debug(f"Could not load episode titles for {imdb_id}: {e}")
            return {}
        self.episode_title_cache[imdb_id] = titles
        return titles

    def _episode_title_identifies(self, item: Dict[str, Any], filename: str) -> bool:
        """Does this filename name this exact episode by title?

        The escape hatch for releases whose numbering cannot be reconciled with
        the provider's. Ordered so the pure string work runs first and the
        database is consulted only for a filename that already looks like a hit.
        """
        episode_title = item.get('episode_title')
        if not episode_title or not filename:
            return False

        series_title = item.get('series_title') or item.get('title')
        matched, _ = episode_title_verdict(episode_title, filename,
                                           series_title=series_title)
        if not matched:
            return False

        show_titles = self._get_episode_titles_cached(item.get('imdb_id'))
        if not show_titles:
            logging.debug(f"Episode-title match declined for '{filename}': no "
                          f"episode-title map for {item.get('imdb_id')}")
            return False

        season = item.get('season_number')
        if season is None:
            season = item.get('season')
        episode = item.get('episode_number')
        if episode is None:
            episode = item.get('episode')
        own = (season, episode)
        others = [t for key, t in show_titles.items() if key != own]

        matched, reason = episode_title_verdict(
            episode_title, filename, other_episode_titles=others,
            series_title=series_title)
        if matched:
            logging.info(f"Episode-title match: '{episode_title}' identifies "
                         f"S{season}E{episode} in '{filename}' despite its numbering")
        else:
            logging.debug(f"Episode-title match declined for '{filename}': {reason}")
        return matched

    def _basenames_already_in_use(self, basenames: List[str], imdb_id: Optional[str],
                                  exclude_item_id: Optional[int] = None) -> set:
        """Of these torrent files, the ones a collected item of this series already owns.

        An in-memory set only covers a single assignment pass, and collisions are
        not built that way: of 413 shared files observed, 145 spanned different
        torrent IDs and 218 different collection times -- one Dragon Ball Z file
        was claimed across three torrents over four months. Each pass started with
        an empty set and saw nothing wrong, so the check has to hit the database.

        Scoped to one imdb_id on purpose. filled_by_file is a bare filename and
        names like '01.mkv' recur across unrelated shows; a global match would
        block a legitimate file that merely shares a name.
        """
        if not basenames or not imdb_id:
            return set()
        try:
            from database.core import get_db_connection
            conn = get_db_connection()
            try:
                in_use = set()
                # SQLite caps bound parameters (999 on older builds). A full
                # series pack can carry more filenames than that, and one
                # oversized IN list would raise and fail this guard open for
                # exactly the packs it matters most for.
                chunk_size = 500
                for start in range(0, len(basenames), chunk_size):
                    chunk = basenames[start:start + chunk_size]
                    placeholders = ','.join('?' * len(chunk))
                    sql = (f"SELECT filled_by_file FROM media_items "
                           f"WHERE imdb_id = ? AND filled_by_file IN ({placeholders}) "
                           f"AND state IN ('Collected','Upgrading')")
                    params = [imdb_id, *chunk]
                    if exclude_item_id is not None:
                        sql += " AND id != ?"
                        params.append(exclude_item_id)
                    in_use.update(row[0] for row in conn.execute(sql, params) if row[0])
                return in_use
            finally:
                conn.close()
        except Exception as e:
            # Fail open. If we cannot prove a file is free, matching behaves as it
            # did before rather than starving episodes on a transient DB error.
            logging.warning(f"Could not determine which files are already in use ({e}); "
                            f"proceeding without the cross-pass collision guard.")
            return set()

    def _get_season_episode_counts_cached(self, tmdb_id: Optional[int]) -> Optional[Dict[int, int]]:
        """Return season→episode-count map cached per tmdb_id."""
        if not tmdb_id:
            return None
        key = str(tmdb_id)
        if key in self.episode_count_cache:
            return self.episode_count_cache[key]
        try:
            from database.database_reading import get_all_season_episode_counts
            counts = get_all_season_episode_counts(tmdb_id)
            if counts:
                self.episode_count_cache[key] = counts
            return counts
        except Exception as e:
            logging.debug(f"Could not fetch season episode counts for tmdb_id={tmdb_id}: {e}")
            return None

    def _compute_absolute_episode_for_item(self, item: Dict[str, Any]) -> Optional[int]:
        """Compute the absolute episode number for an item using cached counts or detect_absolute_numbering."""
        try:
            tmdb_id = item.get('tmdb_id')
            target_season = item.get('season') or item.get('season_number')
            target_episode = item.get('episode') or item.get('episode_number')
            if tmdb_id is None or target_season is None or target_episode is None:
                return None

            series_title = item.get('series_title') or item.get('title')
            uses_absolute, detected_absolute = detect_absolute_numbering(series_title, target_season, target_episode, tmdb_id)
            if uses_absolute and detected_absolute:
                return detected_absolute

            season_episode_counts = self._get_season_episode_counts_cached(tmdb_id)
            if not season_episode_counts:
                return None

            # Season 0 must be excluded. Absolute numbering counts broadcast
            # episodes only, so folding in the specials shifts every absolute
            # number by the length of season 0 -- for Demon Slayer that is 18,
            # turning S05E04's true absolute 59 into 77 and making genuine
            # absolute-numbered releases unmatchable.
            target_absolute_episode = 0
            sorted_seasons = sorted([s for s in season_episode_counts.keys()
                                     if isinstance(s, int) and 0 < s < target_season])
            for s_num in sorted_seasons:
                target_absolute_episode += season_episode_counts.get(s_num, 0)
            target_absolute_episode += target_episode
            return target_absolute_episode
        except Exception as e:
            logging.debug(f"Could not compute absolute episode for item: {e}")
            return None

    def _build_parsed_file_indexes(self, parsed_files: List[Dict[str, Any]]):
        """Build fast lookups for parsed files to avoid scanning all files for every item."""
        by_season_episode = defaultdict(list)  # key: (season or None, episode) -> [parsed_file_info]
        by_episode_only = defaultdict(list)    # key: episode -> [parsed_file_info]
        f1_candidates = {
            'session': [],
            'qualifying': [],
            'race': [],
        }
        date_only_files = []  # Files parsed only as date (no seasons/episodes)

        for parsed_file_info in parsed_files:
            parsed_info = parsed_file_info.get('parsed_info', {})
            if parsed_info.get('is_anime_special_content', False):
                continue

            filename = parsed_info.get('original_filename', '')
            filename_lower = filename.lower()
            if 'session' in filename_lower:
                f1_candidates['session'].append(parsed_file_info)
            if 'qualifying' in filename_lower:
                f1_candidates['qualifying'].append(parsed_file_info)
            if 'race' in filename_lower:
                f1_candidates['race'].append(parsed_file_info)

            seasons = parsed_info.get('seasons') or []
            episodes = parsed_info.get('episodes') or []

            if parsed_info.get('date') and not seasons and not episodes:
                date_only_files.append(parsed_file_info)

            if not episodes:
                continue

            # Index by episode regardless of season
            for ep in episodes:
                by_episode_only[ep].append(parsed_file_info)

            # Index by (season, episode) with season or None
            if seasons:
                for s in seasons:
                    for ep in episodes:
                        by_season_episode[(s, ep)].append(parsed_file_info)
            else:
                for ep in episodes:
                    by_season_episode[(None, ep)].append(parsed_file_info)

        return {
            'by_season_episode': by_season_episode,
            'by_episode_only': by_episode_only,
            'f1_candidates': f1_candidates,
            'date_only_files': date_only_files,
        }

    def _parse_file_info(self, file_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parses a single file's path, checks validity, and returns structured info.

        Args:
            file_dict: Dictionary representing the file {'path': str, 'bytes': int}

        Returns:
            A dictionary with 'path', 'bytes', and 'parsed_info' if valid, otherwise None.
        """
        file_path = file_dict['path']
        file_basename = os.path.basename(file_path)

        if not self.is_video_file(file_basename): # Check basename for extension
            return None
        if 'sample' in file_basename.lower(): # Check basename for 'sample'
            return None
        if 'specials' in file_basename.lower(): # Check basename for 'specials'
             return None
        
        # --- Anime Special Content Detection ---
        # Instead of filtering here, we'll tag it and decide later
        is_anime_special_content = False
        basename_lower = file_basename.lower()
        anime_special_patterns = [
            r'(?<![a-zA-Z0-9])ncop(?=[._-]|$)', r'(?<![a-zA-Z0-9])nced(?=[._-]|$)',  # No Credit Opening/Ending
            r'(?<![a-zA-Z0-9])opening(?=[._-]|$)', r'(?<![a-zA-Z0-9])ending(?=[._-]|$)',
            r'(?<![a-zA-Z0-9])ova(?=[._-]|$)',
            r'(?<![a-zA-Z0-9])blooper(?=[._-]|$)', r'(?<![a-zA-Z0-9])bloopers(?=[._-]|$)',  # Blooper content
            r'(?<=[._-])special(?=[._-]|$)', r'(?<=[._-])specials(?=[._-]|$)',  # Special content (require delimiter before)
            r'(?<![a-zA-Z0-9])omake(?=[._-]|$)', r'(?<![a-zA-Z0-9])omakes(?=[._-]|$)',  # Omake (bonus content)
            r'(?<=[._-])extra(?=[._-]|$)', r'(?<=[._-])extras(?=[._-]|$)',  # Extra content (require delimiter before)
            r'(?<=[._-])bonus(?=[._-]|$)', r'(?<=[._-])bonuses(?=[._-]|$)'  # Bonus content (require delimiter before)
        ]
        
        for i, pattern in enumerate(anime_special_patterns):
            match = re.search(pattern, basename_lower)
            if match:
                is_anime_special_content = True
                logging.debug(f"Tagged as potential anime special content: '{file_basename}' (matched pattern: {pattern})")
                break # Found a match, no need to check others
        

        ptt_result = parse_title(file_basename) # Parse only the basename
        
        # Ensure ptt_result is a dict, even if parse_title returns None or an unexpected type
        parsed_info = ptt_result if isinstance(ptt_result, dict) else {}

        parsed_info['is_anime_special_content'] = is_anime_special_content

        parsed_info['original_filename'] = file_basename # Store basename

        # Attempt fallback episode extraction if PTT fails for episodes
        # PTT might return 'episodes': [] or no 'episodes' key at all.
        # We should trigger fallback if 'episodes' is empty or not present.
        if not parsed_info.get('episodes'): # This covers None or an empty list
             fallback_episode = self._extract_episode_from_filename(file_basename) # Use basename for fallback
             if fallback_episode is not None:
                  parsed_info['fallback_episode'] = fallback_episode
        
        # Additional check for anime openings/endings that might have been parsed as episodes
        # Look for patterns like NCOP1, NCED2, OP1, ED1, etc.
        if parsed_info.get('episodes'):
            episode_list = parsed_info.get('episodes', [])
            original_filename = parsed_info.get('original_filename', '')
            
            # Check if any "episode" numbers are actually opening/ending identifiers
            anime_op_ed_patterns = [
                r'\b(ncop|nced|op|ed)\d+\b',  # NCOP1, NCED2, OP1, ED1, etc.
                r'\bopening\s*\d+\b',         # Opening 1, etc.
                r'\bending\s*\d+\b',          # Ending 1, etc.
            ]
            
            for pattern in anime_op_ed_patterns:
                if re.search(pattern, original_filename.lower()):
                    logging.debug(f"Filtered out anime opening/ending with episode-like numbering: '{original_filename}' (matched pattern: {pattern})")
                    return None
        
        # Debug snapshot of key parsed values to diagnose matching
        try:
            logging.debug(
                f"Parsed file info: name='{file_basename}', date='{parsed_info.get('date')}', "
                f"ymd=({parsed_info.get('year')},{parsed_info.get('month')},{parsed_info.get('day')}), "
                f"seasons={parsed_info.get('seasons')}, episodes={parsed_info.get('episodes')}"
            )
        except Exception:
            pass

        return {
            'path': file_path, # Store original full path
            'bytes': file_dict.get('bytes', 0),
            'parsed_info': parsed_info
        }

    def _official_titles_cached(self, item: Dict[str, Any]) -> Optional[List[str]]:
        """Every name this item may legitimately appear under, or None when that
        cannot be established. None means "no verdict", not "no aliases"."""
        imdb_id = item.get('imdb_id')
        if not imdb_id:
            return None
        if imdb_id in self.official_titles_cache:
            return self.official_titles_cache[imdb_id]

        titles = [item.get('series_title'), item.get('title')]
        alias_count = 0
        try:
            from cli_battery.app.direct_api import DirectAPI
            if (item.get('type') or '').lower() == 'movie':
                aliases, _ = DirectAPI.get_movie_aliases(imdb_id)
            else:
                aliases, _ = DirectAPI.get_show_aliases(imdb_id)
            for country_aliases in (aliases or {}).values():
                for alias in (country_aliases or []):
                    titles.append(alias)
                    alias_count += 1
        except Exception as e:
            logging.debug(f"Alias lookup failed for {imdb_id}: {e}")
            self.official_titles_cache[imdb_id] = None
            return None

        genres = item.get('genres') or []
        if isinstance(genres, str):
            genres = [genres]
        if alias_count == 0 and any('anime' in str(g).lower() for g in genres):
            # Anime with no alias record: releases very likely carry a romaji name
            # the stored title does not contain, so judging them against it alone
            # would reject the only files that exist. No verdict is safer.
            self.official_titles_cache[imdb_id] = None
            return None

        result = [t for t in titles if t]
        self.official_titles_cache[imdb_id] = result
        return result

    def _file_title_agrees(self, ptt_result: Dict[str, Any], item: Dict[str, Any],
                           parsed_file_info: Dict[str, Any]) -> bool:
        """False only when the filename positively names a DIFFERENT show.

        Measured 2026-08-26: PTT returns the EPISODE title for files inside season
        packs - 'S01E03 - The Chelsea Girls.mkv' parses to 'The Chelsea Girls' and
        'Season 1/03 - Weapons of Science.mkv' to 'Weapons of Science'. Those are
        long enough to clear title_verdict's no-assertion floor, so the episode
        title escape hatch below is what keeps legitimate pack members, and is the
        reason this gate reports rather than rejects until proven on real traffic.
        """
        from utilities.settings import get_setting

        official_titles = self._official_titles_cached(item)
        if official_titles is None:
            return True

        ok, score, reason = title_verdict(ptt_result.get('title', ''), official_titles)
        if ok:
            return True

        # Basename only: episode_title_verdict's contract.  A pack folder may
        # list many titles, which would make every file in it look like a hit.
        filename = os.path.basename(
            parsed_file_info.get('path') or ptt_result.get('original_filename')
            or ptt_result.get('original_title') or '')
        if self._episode_title_identifies(item, filename):
            logging.debug(
                f"File title '{ptt_result.get('title')}' disagrees with "
                f"'{item.get('title')}' ({reason}) but the filename names the "
                f"episode - allowing.")
            return True

        logging.warning(
            f"File '{os.path.basename(filename) or filename}' for item "
            f"'{item.get('title')}' S{item.get('season_number')}"
            f"E{item.get('episode_number')}: {reason}"
            + ("" if get_setting('Debug', 'enforce_file_title_match', False)
               else " (reporting only; set Debug.enforce_file_title_match to reject)"))
        return False

    def _season_zero_asserts_special(self, ptt_result: Dict[str, Any], item: Dict[str, Any],
                                     parsed_file_info: Dict[str, Any]) -> bool:
        """True only when a file positively identifies itself as this special.

        Season 0 is the one place where a bare episode number carries no
        information: 'Show - 04.mkv' is episode 4 of the regular run, and the
        index matching it to S00E04 is a coincidence of numbering, not a claim
        about content. Measured 2026-08-26 across 665 collected specials, 660
        were regular episodes filled this way -- and because the slot then read
        as filled, the real OVA was never scraped for. The bad fills were not
        merely junk, they were what prevented the specials being collected.

        Positive evidence is any of: an explicit S00E##/0x## written by the
        release, a special/OVA/movie keyword, or the special's own title from
        metadata appearing in the filename.
        """
        filename = (parsed_file_info.get('path') or ptt_result.get('original_filename')
                    or ptt_result.get('original_title') or '')
        base = os.path.basename(filename) or filename
        if not base:
            return False

        if 0 in (ptt_result.get('seasons') or []):
            return True
        if _SEASON_ZERO_COORD_RE.search(base) or _SPECIAL_ASSERT_RE.search(base):
            return True

        # The special's own title, compared with the show name removed from both
        # sides -- otherwise the shared show name alone carries the score, which
        # is how 'Sword Art Offline II 9' scored 86 against 'Sword Art Online II
        # - 21' and looked like a match.
        ep_title = item.get('episode_title') or ''
        if len(ep_title) >= 4:
            show = item.get('series_title') or item.get('title') or ''
            show_tokens = set(re.sub(r'[^a-z0-9]+', ' ', show.lower()).split())

            def residual(text):
                return ' '.join(t for t in re.sub(r'[^a-z0-9]+', ' ', text.lower()).split()
                                if t and t not in show_tokens and not t.isdigit())

            et, fn = residual(ep_title), residual(base)
            if len(et) >= 4 and len(fn) >= 3 and fuzz.token_set_ratio(et, fn) >= 80:
                return True

        return False

    def _check_match(self, parsed_file_info: Dict[str, Any], item: Dict[str, Any], use_relaxed_matching: bool, xem_mapping: Optional[Dict[str, int]] = None) -> bool:
        """
        Checks if a pre-parsed file info dictionary matches a media item (TV Episode logic).

        Args:
            parsed_file_info: The dictionary returned by _parse_file_info.
            item: The media item (episode) to match against.
            use_relaxed_matching: Flag for relaxed matching rules.
            xem_mapping: Optional dictionary with 'season' and 'episode' keys from XEM.

        Returns:
            True if the file matches the item, False otherwise.
        """
        # Always reject anime special content files (bloopers, openings, endings, etc.)
        is_special_content = parsed_file_info.get('parsed_info', {}).get('is_anime_special_content', False)
        if is_special_content:
            logging.debug(f"Rejecting anime special content file: '{parsed_file_info.get('path', 'unknown')}'")
            return False

        ptt_result = parsed_file_info['parsed_info'] # Get the PTT result stored earlier

        # A film inside a series pack must not fill a numbered episode. Anime
        # collections routinely ship "<Show> The Movie 03" alongside the episodes,
        # and PTT reads that trailing number as episode 3, so the movie lands in
        # the episode-only index and matches on the number alone - measured
        # 2026-08-26, Naruto S1E3 and S1E4 were filled with Shippuden films.
        # Season 0 is exempt: specials are where films legitimately belong, and
        # that is the only place they appear across 28,893 collected episodes.
        _target_season_for_movie_check = item.get('season') or item.get('season_number')
        if _MOVIE_TITLE_RE.search(ptt_result.get('title') or '') and _target_season_for_movie_check not in (0, '0'):
            logging.debug(
                f"Rejecting '{ptt_result.get('original_filename', '')}' for "
                f"{item.get('title')} S{_target_season_for_movie_check}"
                f"E{item.get('episode_number')}: its title names a film, not an episode.")
            return False

        # A file whose name states a DIFFERENT show is not this episode, however
        # well its numbers line up. Nothing below this point compares titles at
        # all: on 2026-08-26 a file named 'House of the Dragon S01E03 ...' was
        # symlinked into a Dr. Stone entry on an S1E3 coordinate match alone.
        # This is a backstop - filter_results is the primary gate - so it only
        # reports by default. See _file_title_agrees for why.
        if not self._file_title_agrees(ptt_result, item, parsed_file_info):
            from utilities.settings import get_setting as _get_setting
            if _get_setting('Debug', 'enforce_file_title_match', False):
                return False

        # Determine target season/episode: Use XEM if available, otherwise original item S/E
        target_season = item.get('season') or item.get('season_number')
        target_episode = item.get('episode') or item.get('episode_number')
        using_xem = False
        if xem_mapping and 'season' in xem_mapping and 'episode' in xem_mapping:
             # Validate XEM values are integers
             try:
                 xem_season = int(xem_mapping['season'])
                 xem_episode = int(xem_mapping['episode'])
                 logging.debug(f"Using XEM mapping for match check: S{xem_season}E{xem_episode} (Original: S{target_season}E{target_episode})")
                 target_season = xem_season
                 target_episode = xem_episode
                 using_xem = True
             except (ValueError, TypeError):
                  logging.warning(f"Invalid XEM mapping format encountered: {xem_mapping}. Falling back to original item S/E.")
                  # Fallback to original item S/E below
        # else: # No need for else, target_season/episode already hold original values
        #      logging.debug(f"Using original item S/E for match check: S{target_season}E{target_episode}")


        # Season 0 fills only on positive evidence. See
        # _season_zero_asserts_special for the measurement behind this.
        if target_season in (0, '0'):
            from utilities.settings import get_setting as _get_setting
            if _get_setting('Debug', 'strict_season_zero_match', True):
                if not self._season_zero_asserts_special(ptt_result, item, parsed_file_info):
                    logging.info(
                        f"Season 0 rejected for '{item.get('title')}' S00E{target_episode}: "
                        f"'{os.path.basename(parsed_file_info.get('path') or '')}' does not "
                        f"identify itself as a special (bare episode number is not evidence).")
                    return False

        # Check required item fields (use target season/episode now)
        series_title = item.get('series_title', '') or item.get('title', '')
        if not all([series_title, target_episode is not None]):
            logging.debug(f"Match failed: Missing series title or target episode ({target_episode})")
            return False
        # Relaxed matching doesn't strictly require season, but strict does IF NOT using XEM
        if not use_relaxed_matching and target_season is None and not using_xem:
            logging.debug(f"Match failed: Strict matching requires season, but item season is None and not using XEM.")
            return False

        # --- Check if this is anime content ---
        genres = item.get('genres') or []
        if isinstance(genres, str):
            genres = [genres]
        is_anime = any('anime' in genre.lower() for genre in genres)

        # Formula 1 has a bespoke year/event matcher below.  Every ordinary TV
        # episode uses the same pair-atomic identity authority as scrape-time
        # filtering, so the selected torrent file cannot reintroduce a result
        # that the scraper correctly rejected.
        is_formula_1 = ('formula 1' in series_title.lower()
                        and 'drive to survive' not in series_title.lower())
        if not is_formula_1:
            file_numbers = list(ptt_result.get('episodes') or [])
            if not file_numbers and ptt_result.get('fallback_episode') is not None:
                file_numbers = [ptt_result['fallback_episode']]

            stored_season = item.get('season')
            if stored_season is None:
                stored_season = item.get('season_number')
            stored_episode = item.get('episode')
            if stored_episode is None:
                stored_episode = item.get('episode_number')
            coordinates = [(target_season, target_episode)]
            if (stored_season, stored_episode) != (target_season, target_episode):
                coordinates.append((stored_season, stored_episode))

            show_titles = self._get_episode_titles_cached(item.get('imdb_id'))
            own_coordinate = (stored_season, stored_episode)
            other_titles = [value for key, value in show_titles.items()
                            if key != own_coordinate]
            file_path = (parsed_file_info.get('path')
                         or ptt_result.get('original_filename') or '')
            # The verdict's filename contract is basename only: folder text
            # would both leak sibling episode titles into the title haystack
            # and turn folder tokens like "S01 - 1080p" into bogus explicit
            # coordinates for every file inside.  The full path is kept solely
            # for the container-season reading, which is folder semantics.
            filename = os.path.basename(file_path) or file_path
            identity_match, identity_reason = episode_identity_verdict(
                target_coordinates=coordinates,
                file_seasons=ptt_result.get('seasons'),
                file_numbers=file_numbers,
                filename=filename,
                absolute_episode=self._compute_absolute_episode_for_item(item)
                if is_anime else None,
                container_season=container_season_from_path(file_path),
                is_anime=is_anime,
                target_air_date=item.get('release_date'),
                file_air_date=ptt_result.get('date'),
                episode_title=item.get('episode_title'),
                other_episode_titles=other_titles,
                series_title=series_title,
                allow_bare_season_one=use_relaxed_matching,
            )
            if identity_match:
                logging.debug(
                    f"Episode identity accepted '{filename}' for "
                    f"{coordinates}: {identity_reason}"
                )
                return True
            logging.info(
                f"Episode identity REJECTED '{filename}' for "
                f"{coordinates}: {identity_reason}"
            )
            return False

        # --- Relaxed Matching Logic ---
        if use_relaxed_matching:
            episode_match = False
            # Check PTT episodes
            if ptt_result.get('episodes') and target_episode in ptt_result.get('episodes', []):
                episode_match = True
                logging.debug("Relaxed match: PTT episode matched target episode.")
            # Check fallback episode if PTT episodes are empty
            elif not ptt_result.get('episodes') and ptt_result.get('fallback_episode') == target_episode:
                episode_match = True
                logging.debug("Relaxed match: Fallback episode matched target episode.")

            # --- Season matching -------------------------------------------
            # One authority, shared with the strict branch below and with
            # filter_results. See scraper/functions/season_resolution.py for why
            # the previous four-pattern version let a season-1 batch satisfy
            # every season of a show.
            file_numbers = list(ptt_result.get('episodes') or [])
            if not file_numbers and ptt_result.get('fallback_episode') is not None:
                file_numbers = [ptt_result['fallback_episode']]

            absolute_episode = None
            if is_anime:
                # Use the XEM-remapped S/E, which is what target_* already hold.
                item_for_abs = dict(item)
                item_for_abs['season'] = item_for_abs['season_number'] = target_season
                item_for_abs['episode'] = item_for_abs['episode_number'] = target_episode
                absolute_episode = self._compute_absolute_episode_for_item(item_for_abs)

            season_match, season_reason = season_verdict(
                file_seasons=ptt_result.get('seasons'),
                target_season=target_season,
                file_numbers=file_numbers,
                absolute_episode=absolute_episode,
                container_season=container_season_from_path(parsed_file_info.get('path', '')),
                is_anime=is_anime,
            )
            log_verdict(season_match, season_reason,
                        ptt_result.get('original_filename', ''), target_season, target_episode)

            # An absolute-numbered release names the absolute episode, not the
            # in-season one, so the episode check has to accept that number too.
            if season_match and not episode_match and absolute_episode is not None \
                    and absolute_episode in file_numbers:
                episode_match = True
                logging.debug(f"Episode matched via absolute number {absolute_episode}")
            # --- End season matching ---------------------------------------

            # --- ORIGINAL EPISODE FALLBACK (similar to filter_results.py) ---
            # If we're using XEM mapping and the episode number changed, try the original episode as fallback
            if not episode_match and using_xem:
                original_item_season = item.get('season') or item.get('season_number')
                original_item_episode = item.get('episode') or item.get('episode_number')
                
                # Only try fallback if original episode is different from target episode
                if original_item_episode is not None and original_item_episode != target_episode:
                    logging.debug(f"Trying original episode fallback: original_episode={original_item_episode}, xem_episode={target_episode}, torrent_episodes={ptt_result.get('episodes', [])}")
                    
                    # Try matching against the original episode number
                    if original_item_episode in ptt_result.get('episodes', []):
                        episode_match = True
                        logging.info(f"Episode matched via original episode number {original_item_episode} for '{ptt_result.get('original_filename', '')}'")
                    elif ptt_result.get('original_filename') and re.search(rf'\b{original_item_episode}\b', ptt_result.get('original_filename')):
                        episode_match = True
                        logging.info(f"Episode matched via original episode number {original_item_episode} found in filename for '{ptt_result.get('original_filename', '')}'")
            # --- End original episode fallback ---

            # The numbering could not place this file. If the filename names the
            # episode by title, that identifies it more precisely than any
            # number does -- see season_resolution.episode_title_verdict.
            if not (season_match and episode_match) and self._episode_title_identifies(
                    item, ptt_result.get('original_filename', '')):
                season_match = episode_match = True

            if season_match and episode_match:
                 logging.debug(f"Relaxed match successful: S:{season_match} E:{episode_match}")
                 return True
            else:
                 logging.debug(f"Relaxed match failed: S:{season_match} E:{episode_match}")
                 return False

        # --- Strict Matching Logic ---
        else:
            # Check date-based first (if file parsed as date-based)
            date_match = False
            has_date = 'date' in ptt_result and ptt_result['date'] is not None
            has_seasons = bool(ptt_result.get('seasons'))
            has_episodes = bool(ptt_result.get('episodes'))

            if has_date and not has_seasons and not has_episodes:
                 try:
                      # Use original item S/E for TMDB lookup as that identifies the actual episode
                      original_item_season = item.get('season') or item.get('season_number')
                      original_item_episode = item.get('episode') or item.get('episode_number')
                      if item.get('tmdb_id') and original_item_season is not None and original_item_episode is not None:
                           from utilities.web_scraper import get_tmdb_data
                           episode_data = get_tmdb_data(int(item['tmdb_id']), 'tv', original_item_season, original_item_episode)
                           logging.debug(
                               f"Date-only file: comparing file_date='{ptt_result.get('date')}' with TMDB='{episode_data.get('air_date') if episode_data else None}' "
                               f"for tmdb_id={item.get('tmdb_id')} S{original_item_season}E{original_item_episode}"
                           )
                           if episode_data and episode_data.get('air_date') == ptt_result['date']:
                                logging.debug("Strict match: Date matched via TMDB lookup.")
                                date_match = True
                           else:
                                logging.debug(f"Strict match: Date mismatch (File: {ptt_result['date']}, TMDB: {episode_data.get('air_date') if episode_data else 'N/A'})")
                      else:
                           logging.debug("Strict match: Skipping date check (missing TMDB ID/S/E for lookup).")
                 except Exception as e:
                      logging.warning(f"Could not perform date check during match: {e}") # Warn instead of error

            # Season/Episode matching (using target_season/target_episode)
            season_episode_match = False
            if not date_match: # Only check if date didn't match
                # Determine item title for F1 check
                item_title_for_f1_check = (item.get('series_title', '') or item.get('title', '')).lower()
                # Treat as Formula 1 motorsport event only if title includes "formula 1" **and** does NOT contain
                # "drive to survive" (which refers to the Netflix documentary series).
                is_formula_1_item = ("formula 1" in item_title_for_f1_check) and ("drive to survive" not in item_title_for_f1_check)

                if is_formula_1_item and not using_xem: # Apply F1 logic if not overridden by XEM
                    # For F1, target_season IS the event year.
                    # We check if the file's PTT season is typical for F1 (empty or S1).
                    # The year from the filename's PTT is not reliable here.
                    season_match = (not ptt_result.get('seasons') or ptt_result.get('seasons') == [1])
                    
                    # Episode match for F1: item's event number should be in filename's PTT episodes,
                    # or filename has no PTT episodes (e.g. single file for the whole event part).
                    episode_match = (target_episode is None or not ptt_result.get('episodes') or target_episode in ptt_result.get('episodes', []))
                    
                    logging.debug(f"Strict F1 match: S/E check -> S:{season_match} E:{episode_match} (Item S{target_season}E{target_episode}, FilePTTSeason: {ptt_result.get('seasons')}, FilePTTEpisodes: {ptt_result.get('episodes')})")
                else: # Original logic for non-F1 or if using XEM
                    # Same authority as the relaxed branch, but strict mode
                    # does not accept a file that names no season anywhere:
                    # allow_bare_season_one=False keeps its existing contract
                    # that the season must be stated in the name or the folder.
                    file_numbers = list(ptt_result.get('episodes') or [])
                    if not file_numbers and ptt_result.get('fallback_episode') is not None:
                        file_numbers = [ptt_result['fallback_episode']]

                    absolute_episode = None
                    if is_anime:
                        item_for_abs = dict(item)
                        item_for_abs['season'] = item_for_abs['season_number'] = target_season
                        item_for_abs['episode'] = item_for_abs['episode_number'] = target_episode
                        absolute_episode = self._compute_absolute_episode_for_item(item_for_abs)

                    if using_xem and target_season is None and not ptt_result.get('seasons'):
                        season_match, season_reason = True, 'XEM mapped to no season'
                    else:
                        season_match, season_reason = season_verdict(
                            file_seasons=ptt_result.get('seasons'),
                            target_season=target_season,
                            file_numbers=file_numbers,
                            absolute_episode=absolute_episode,
                            container_season=container_season_from_path(
                                parsed_file_info.get('path', '')),
                            is_anime=is_anime,
                            allow_bare_season_one=False,
                        )
                    log_verdict(season_match, season_reason,
                                ptt_result.get('original_filename', ''),
                                target_season, target_episode)

                    episode_match = target_episode in file_numbers
                    if season_match and not episode_match and absolute_episode is not None \
                            and absolute_episode in file_numbers:
                        episode_match = True
                        logging.debug(f"Strict: episode matched via absolute {absolute_episode}")

                    # --- ORIGINAL EPISODE FALLBACK FOR STRICT MODE (similar to filter_results.py) ---
                    # If we're using XEM mapping and the episode number changed, try the original episode as fallback
                    if not episode_match and using_xem:
                        original_item_season = item.get('season') or item.get('season_number')
                        original_item_episode = item.get('episode') or item.get('episode_number')
                        
                        # Only try fallback if original episode is different from target episode
                        if original_item_episode is not None and original_item_episode != target_episode:
                            logging.debug(f"Trying original episode fallback (strict): original_episode={original_item_episode}, xem_episode={target_episode}, torrent_episodes={ptt_result.get('episodes', [])}")
                            
                            # Try matching against the original episode number
                            if original_item_episode in ptt_result.get('episodes', []):
                                episode_match = True
                                logging.info(f"Episode matched via original episode number {original_item_episode} for '{ptt_result.get('original_filename', '')}' (strict mode)")
                            elif ptt_result.get('original_filename') and re.search(rf'\b{original_item_episode}\b', ptt_result.get('original_filename')):
                                episode_match = True
                                logging.info(f"Episode matched via original episode number {original_item_episode} found in filename for '{ptt_result.get('original_filename', '')}' (strict mode)")
                    # --- End original episode fallback for strict mode ---

                season_episode_match = season_match and episode_match
                # Same escape hatch as the relaxed branch: a filename that names
                # the episode by title identifies it regardless of numbering.
                if not season_episode_match and self._episode_title_identifies(
                        item, ptt_result.get('original_filename', '')):
                    season_episode_match = True
                logging.debug(f"Strict match S/E component result: {season_episode_match}")


                # Last resort: check file date against TMDB date for season/episode files if S/E match failed
                if not season_episode_match and ptt_result.get('date'):
                    try:
                         # Use original item S/E for TMDB lookup
                         original_item_season = item.get('season') or item.get('season_number')
                         original_item_episode = item.get('episode') or item.get('episode_number')
                         if item.get('tmdb_id') and original_item_season is not None and original_item_episode is not None:
                             from utilities.web_scraper import get_tmdb_data
                             episode_data = get_tmdb_data(int(item['tmdb_id']), 'tv', original_item_season, original_item_episode)
                             logging.debug(
                                 f"Fallback date compare: file_date='{ptt_result.get('date')}', TMDB='{episode_data.get('air_date') if episode_data else None}' "
                                 f"for tmdb_id={item.get('tmdb_id')} S{original_item_season}E{original_item_episode}"
                             )
                             if episode_data and episode_data.get('air_date') == ptt_result['date']:
                                 logging.debug("Strict match: Date matched via fallback TMDB lookup.")
                                 date_match = True # Consider it a date match if air dates align
                             else:
                                 logging.debug(f"Strict match: Fallback date mismatch (File: {ptt_result['date']}, TMDB: {episode_data.get('air_date') if episode_data else 'N/A'})")
                         else:
                              logging.debug("Strict match: Skipping fallback date check (missing TMDB ID/S/E).")
                    except Exception as e:
                         logging.warning(f"Could not perform fallback date check: {e}")

            final_match = season_episode_match or date_match
            logging.debug(f"Strict match final result: {final_match}")
            return final_match

    def find_best_match_from_parsed(self, parsed_files: List[Dict[str, Any]], item: Dict[str, Any], xem_mapping: Optional[Dict[str, int]] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        Finds the best matching file for a single item from a list of pre-parsed file info.

        Args:
            parsed_files: List of dictionaries returned by _parse_file_info.
            item: The media item to match.
            xem_mapping: Optional dictionary with 'season' and 'episode' keys from XEM.

        Returns:
            A tuple (matching_filepath_basename, item) if a match is found, otherwise None.
            For movies, returns the largest video file path basename.
            For episodes, returns the first file that matches season/episode criteria (using XEM if provided).
        """
        item_type = item.get('type')

        # --- Movie Logic (Find largest video file) ---
        if item_type == 'movie':
            video_files = []
            for parsed_file in parsed_files:
                 # _parse_file_info already filtered non-video/samples
                 video_files.append(parsed_file)

            if not video_files:
                return None

            # Largest-file is the right heuristic for a single-film torrent, where
            # the extras are smaller, but it is wrong for a collection: a "Harry
            # Potter (2001-2011) 4K Collection" contains eight films and the
            # largest is whichever encoded biggest. Measured 2026-08-26, that
            # filled Philosopher's Stone with Chamber of Secrets.
            #
            # So when more than one file could be a feature, prefer the one whose
            # own name matches this item, and fall back to size only if nothing
            # names it - a file that asserts no title is still better than a file
            # that asserts a different one.
            # None from _official_titles_cached means 'no verdict possible' (the
            # alias lookup failed, or this is anime with no alias record), which
            # everywhere else means DO NOT judge. Falling back to the stored title
            # alone there is exactly the case documented as unsafe, so skip the
            # naming preference entirely and let size decide.
            official_titles = self._official_titles_cached(item)
            if len(video_files) > 1 and official_titles:
                named = []
                for pf in video_files:
                    cand = (pf.get('parsed_info') or {}).get('title') or ''
                    # A file that asserts no title cannot be used to pick between
                    # films: title_verdict fails open at 1.0 for those, which would
                    # sort every bare-numbered file ('01.mkv') above the file that
                    # actually names this item and silently reinstate the
                    # largest-file bug this block exists to fix.
                    if not title_is_asserted(cand):
                        continue
                    ok, score, _ = title_verdict(cand, official_titles)
                    if ok and score >= MIN_TITLE_MATCH:
                        named.append((score, pf.get('bytes', 0), pf))
                if named:
                    named.sort(key=lambda t: (t[0], t[1]), reverse=True)
                    best = named[0]
                    if len(named) < len(video_files):
                        logging.info(
                            f"Movie pack: {len(video_files)} video files, {len(named)} name "
                            f"'{item.get('title')}'; taking "
                            f"'{os.path.basename(best[2]['path'])}' (title {best[0]:.2f})")
                    return (os.path.basename(best[2]['path']), item)
                logging.warning(
                    f"Movie pack: none of {len(video_files)} files name "
                    f"'{item.get('title')}'; falling back to the largest.")

            # Sort by size descending and take the largest
            largest_file_info = max(video_files, key=lambda x: x.get('bytes', 0))
            return (os.path.basename(largest_file_info['path']), item) # Return basename path and item

        # --- TV Episode Logic ---
        elif item_type == 'episode':
            # Build indexes once for faster candidate selection
            indexes = self._build_parsed_file_indexes(parsed_files)
            by_season_episode = indexes['by_season_episode']
            by_episode_only = indexes['by_episode_only']
            f1_candidates = indexes['f1_candidates']
            try:
                logging.debug(
                    f"Parsed files summary: total={len(parsed_files)}, date_only={len(indexes.get('date_only_files', []))}"
                )
            except Exception:
                pass
            # Check for Formula 1
            item_title_for_f1_check = (item.get('series_title', '') or item.get('title', '')).lower()
            # Same refined detection as above to avoid mis-classifying "Formula 1: Drive to Survive".
            is_formula_1_item = ("formula 1" in item_title_for_f1_check) and ("drive to survive" not in item_title_for_f1_check)

            if is_formula_1_item:
                logging.debug(f"Formula 1 item detected: '{item_title_for_f1_check}'. Applying simplified 'session' file matching.")
                # Prefer pre-indexed F1 candidates first
                candidate_sets = [
                    f1_candidates.get('session', []),
                    f1_candidates.get('qualifying', []),
                    f1_candidates.get('race', []),
                ]
                # Iterate candidates in priority order
                for candidate_list in candidate_sets:
                    for parsed_file_info in candidate_list:
                        parsed_info_dict = parsed_file_info.get('parsed_info', {})
                        original_filename = parsed_info_dict.get('original_filename', '')
                        if "session" in original_filename.lower() or "qualifying" in original_filename.lower() or "race" in original_filename.lower():
                            logging.info(f"F1 Match (simplified): Found candidate '{original_filename}'. Matching item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')} to file: {parsed_file_info['path']}")
                            return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item
                # Fallback: scan remaining parsed files for any missed F1 indicators
                for parsed_file_info in parsed_files:
                    # Ensure 'parsed_info' and 'original_filename' exist
                    parsed_info_dict = parsed_file_info.get('parsed_info', {})
                    original_filename = parsed_info_dict.get('original_filename', '')
                    
                    if "session" in original_filename.lower():
                        logging.info(f"F1 Match (simplified): Found 'session' in filename '{original_filename}'. Matching item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')} to file: {parsed_file_info['path']}")
                        return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item
                    elif "qualifying" in original_filename.lower():
                        logging.info(f"F1 Match (simplified): Found 'qualifying' in filename '{original_filename}'. Matching item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')} to file: {parsed_file_info['path']}")
                        return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item
                    elif "race" in original_filename.lower():
                        logging.info(f"F1 Match (simplified): Found 'race' in filename '{original_filename}'. Matching item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')} to file: {parsed_file_info['path']}")
                        return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item
                    
                
                logging.info(f"F1 Match (simplified): No file containing 'session'/'qualifying'/'race' found for item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')}. No match by this specific F1 rule.")
                return None # No file with "session" found for this F1 item by this rule

            # Determine if relaxed matching should be used (copied from original _match_tv_content)
            genres = item.get('genres') or []
            if isinstance(genres, str):
                genres = [genres]
            is_anime = any('anime' in genre.lower() for genre in genres)
            from utilities.settings import get_setting
            file_collection_management = get_setting('File Management', 'file_collection_management')
            using_plex = file_collection_management == 'Plex'
            use_relaxed_matching = not using_plex and (is_anime or self.relaxed_matching)
            logging.debug(f"Episode matching mode: {'Relaxed' if use_relaxed_matching else 'Strict'}")

            # Narrow candidates by season/episode indexes to avoid scanning all files
            target_season = item.get('season') or item.get('season_number')
            target_episode = item.get('episode') or item.get('episode_number')
            stored_season, stored_episode = target_season, target_episode

            # Apply XEM mapping from item if available (similar to how scraper.py does it)
            if xem_mapping:
                try:
                    xem_season = int(xem_mapping.get('season'))
                    xem_episode = int(xem_mapping.get('episode'))
                    logging.debug(f"Using XEM mapping for media matching: S{xem_season}E{xem_episode} (Original: S{target_season}E{target_episode})")
                    target_season = xem_season
                    target_episode = xem_episode
                except (ValueError, TypeError):
                    logging.warning(f"Invalid XEM mapping format in media matcher: {xem_mapping}. Using original S/E.")

            candidate_files: List[Dict[str, Any]] = []
            seen_ids = set()
            if target_episode is not None:
                # Index hits: (season, episode)
                for pf in by_season_episode.get((target_season, target_episode), []):
                    if id(pf) not in seen_ids:
                        seen_ids.add(id(pf)); candidate_files.append(pf)
                # Index hits: (None, episode) -- files whose name carries no season.
                for pf in by_season_episode.get((None, target_episode), []):
                    if id(pf) not in seen_ids:
                        seen_ids.add(id(pf)); candidate_files.append(pf)
                # Index hits: episode-only.
                # These indexes exist purely to avoid scanning every file; they
                # deliberately carry NO season authority. A previous version
                # filtered the (None, episode) list on the containing folder and
                # then re-added every excluded file here one loop later, so the
                # filter did nothing. _check_match is the only thing that decides.
                for pf in by_episode_only.get(target_episode, []):
                    if id(pf) not in seen_ids:
                        seen_ids.add(id(pf)); candidate_files.append(pf)
                # A mapped coordinate (XEM or Cinemeta) narrows the lookup, but
                # the files themselves are indexed by their RAW parsed numbers.
                # An anime stored as S1E25 whose resolver answers S2E1 would
                # otherwise never even see 'Show - 25.mkv' as a candidate, and
                # fail to match its own torrent.  _check_match already accepts
                # both pairs, so also offer the stored coordinate's buckets.
                if (stored_season, stored_episode) != (target_season, target_episode) \
                        and stored_episode is not None:
                    for pf in by_season_episode.get((stored_season, stored_episode), []):
                        if id(pf) not in seen_ids:
                            seen_ids.add(id(pf)); candidate_files.append(pf)
                    for pf in by_season_episode.get((None, stored_episode), []):
                        if id(pf) not in seen_ids:
                            seen_ids.add(id(pf)); candidate_files.append(pf)
                    for pf in by_episode_only.get(stored_episode, []):
                        if id(pf) not in seen_ids:
                            seen_ids.add(id(pf)); candidate_files.append(pf)
                # Anime absolute-numbered files, mirroring find_related_items.
                if is_anime:
                    abs_ep = self._compute_absolute_episode_for_item(item)
                    if abs_ep is not None:
                        for pf in by_episode_only.get(abs_ep, []):
                            if id(pf) not in seen_ids:
                                seen_ids.add(id(pf)); candidate_files.append(pf)
                        for pf in by_season_episode.get((1, abs_ep), []):
                            if id(pf) not in seen_ids:
                                seen_ids.add(id(pf)); candidate_files.append(pf)
            else:
                candidate_files = parsed_files  # Fallback if no episode available

            # Always include date-only files as candidates so strict date matching can run
            try:
                date_only_list = indexes.get('date_only_files', [])
                for pf in date_only_list:
                    if id(pf) not in seen_ids:
                        seen_ids.add(id(pf)); candidate_files.append(pf)
                if date_only_list:
                    logging.debug(f"Included {len(date_only_list)} date-only parsed files as candidates for matching.")
            except Exception:
                pass

            for parsed_file_info in candidate_files:
                # Always skip files that are tagged as anime special content
                is_special = parsed_file_info.get('parsed_info', {}).get('is_anime_special_content', False)
                if is_special:
                    logging.debug(f"Skipping anime special file '{parsed_file_info['path']}' for episode matching.")
                    continue

                # Pass xem_mapping down to _check_match
                if self._check_match(parsed_file_info, item, use_relaxed_matching, xem_mapping=xem_mapping):
                    # Return the first match found
                    logging.info(f"Match found for item '{item.get('title')}' S{target_season}E{target_episode} (using XEM: {xem_mapping is not None}) -> File: {parsed_file_info['path']}")
                    return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item

            # Last resort: the indexes above are keyed on the episode NUMBER, so
            # a release that renumbers wholesale is not even offered as a
            # candidate -- a Pokemon pack calling S24E42 'S19E90' never reaches
            # _check_match, and its episode-title escape hatch never runs.
            # Scanning the rest is only worth it when the item's title can
            # identify an episode by itself, so the cost falls on failures only.
            if episode_title_is_usable(item.get('episode_title') or ''):
                already_tried = {id(pf) for pf in candidate_files}
                for parsed_file_info in parsed_files:
                    if id(parsed_file_info) in already_tried:
                        continue
                    parsed_info = parsed_file_info.get('parsed_info', {})
                    if parsed_info.get('is_anime_special_content', False):
                        continue
                    if self._episode_title_identifies(
                            item, parsed_info.get('original_filename', '')):
                        logging.info(
                            f"Match found by episode title for item '{item.get('title')}' "
                            f"S{target_season}E{target_episode} -> File: {parsed_file_info['path']}")
                        return (os.path.basename(parsed_file_info['path']), item)

            try:
                logging.debug(
                    f"No matching file found for item '{item.get('title')}' S{target_season}E{target_episode} (using XEM: {xem_mapping is not None}) in parsed files. "
                    f"Candidates considered: {len(candidate_files)}"
                )
            except Exception:
                logging.debug(f"No matching file found for item '{item.get('title')}' S{target_season}E{target_episode} (using XEM: {xem_mapping is not None}) in parsed files.")
            return None # No match found

        # --- Unknown Type ---
        else:
            logging.warning(f"Unknown item type '{item_type}' in find_best_match_from_parsed")
            return None

    def _extract_episode_from_filename(self, filename: str) -> Optional[int]:
        """
        Fallback method to extract episode numbers from filenames when PTT fails.
        Handles cases like '999 1.mp4' or 'ep1.mp4'
        """
        # Remove the file extension
        basename = os.path.splitext(os.path.basename(filename))[0]
        
        # Skip special content files to avoid false episode extraction
        basename_lower = basename.lower()
        special_content_patterns = [
            r'(?<![a-zA-Z0-9])ncop(?=[._-]|$)', r'(?<![a-zA-Z0-9])nced(?=[._-]|$)', r'(?<![a-zA-Z0-9])opening(?=[._-]|$)', r'(?<![a-zA-Z0-9])ending(?=[._-]|$)', r'(?<![a-zA-Z0-9])ova(?=[._-]|$)',
            r'(?<![a-zA-Z0-9])blooper(?=[._-]|$)', r'(?<![a-zA-Z0-9])bloopers(?=[._-]|$)', r'(?<![a-zA-Z0-9])special(?=[._-]|$)', r'(?<![a-zA-Z0-9])specials(?=[._-]|$)',
            r'(?<![a-zA-Z0-9])omake(?=[._-]|$)', r'(?<![a-zA-Z0-9])omakes(?=[._-]|$)', r'(?<![a-zA-Z0-9])extra(?=[._-]|$)', r'(?<![a-zA-Z0-9])extras(?=[._-]|$)',
            r'(?<![a-zA-Z0-9])bonus(?=[._-]|$)', r'(?<![a-zA-Z0-9])bonuses(?=[._-]|$)'
        ]
        
        for pattern in special_content_patterns:
            if re.search(pattern, basename_lower):
                logging.debug(f"Skipping episode extraction for special content file: '{filename}' (matched pattern: {pattern})")
                return None

        # Try various patterns, but be more specific to avoid false positives
        patterns = [
            # Most specific patterns first
            r'(?:ep|episode)[.\s-]*(\d{1,4})(?:\D|$)',  # Matches "ep1" or "episode 1" (most reliable)
            r'[eE](\d{1,4})(?:\D|$)',  # Matches "E1" or "e01" (word boundary)
            
            # Standalone numbers - be more careful to avoid season numbers
            r'(?:^|\D)(\d{1,4})(?:\D|$)',  # Matches standalone numbers like "1" or "001"
        ]

        for pattern in patterns:
            match = re.search(pattern, basename)
            if match:
                try:
                    episode_num = int(match.group(1))
                    if 0 < episode_num < 2000:  # Sanity check for reasonable episode numbers
                        
                        # Additional context check for standalone numbers to avoid season conflicts
                        if pattern == r'(?:^|\D)(\d{1,4})(?:\D|$)':
                            # For standalone numbers, check if it's likely part of a season number
                            start_pos = match.start()
                            end_pos = match.end()
                            
                            # Check if preceded by 's' or 'season' (likely a season number)
                            if start_pos > 0:
                                before_match = basename[start_pos-1:start_pos+1]
                                if before_match.startswith('s') or before_match.startswith('season'):
                                    continue
                            
                            # Check if followed by 'e' (likely part of SxxExx format)
                            if end_pos < len(basename):
                                after_match = basename[end_pos-1:end_pos+1]
                                if after_match.endswith('e'):
                                    continue
                        
                        return episode_num
                except ValueError:
                    continue

        return None

    def match_movie(self, parsed: Dict[str, Any], item: Dict[str, Any], filename: str) -> bool:
        """
        Check if a movie file matches a movie item.
        Matches based on the queue item title and year.
        NOTE: This uses PTT parsed info, assumes 'parsed' is the result of PTT.
              It's likely NOT used directly by AddingQueue flow anymore.
        """
        # Get the parsed title from the file
        parsed_title = self._normalize_title(parsed.get('title', ''))
        queue_title = self._normalize_title(item.get('title', ''))
        if not parsed_title or not queue_title:
            return False

        # Match based on normalized title match using fuzzy matching for robustness
        # title_match = parsed_title == queue_title
        title_ratio = fuzz.ratio(parsed_title, queue_title)
        title_match = title_ratio > 85 # Use a threshold (e.g., 85%) for title match

        # Be lenient about year matching - only check if both years are present
        year_match = self._is_acceptable_year_mismatch(item, parsed)

        # Match only if both title and year match
        logging.debug(f"Movie match check: '{parsed_title}' vs '{queue_title}' (Ratio: {title_ratio}), Year Match: {year_match} -> Result: {title_match and year_match}")
        return title_match and year_match

    def match_episode(self, parsed: Dict[str, Any], item: Dict[str, Any]) -> bool:
        """
        Check if an episode file matches an episode item.
        Matches based on the queue item title, season, and episode numbers.
        NOTE: This uses PTT parsed info, assumes 'parsed' is the result of PTT.
              It's likely NOT used directly by AddingQueue flow anymore.
        """
        # Skip files that are likely extras/specials based on filename from PTT result
        original_filename = parsed.get('original_filename', '').lower() # Use stored basename
        if any(extra in original_filename for extra in [
            'deleted scene', 'deleted scenes',
            'extra', 'extras',
            'special', 'specials',
            'behind the scene', 'behind the scenes',
            'bonus', 'interview',
            'featurette', 'making of',
            'alternate'
        ]):
            return False

        # Traditional matching
        parsed_title = self._normalize_title(parsed.get('title', ''))
        queue_title = self._normalize_title(item.get('series_title', '') or item.get('title', ''))
        if not parsed_title or not queue_title:
            return False

        # Title match using fuzzy matching
        # title_match = parsed_title == queue_title
        title_ratio = fuzz.ratio(parsed_title, queue_title)
        title_match = title_ratio > 85 # Use a threshold

        # Get season/episode from item
        item_season = item.get('season') or item.get('season_number')
        item_episode = item.get('episode') or item.get('episode_number')

        if item_season is None or item_episode is None:
            return False

        # Check if the requested season and episode are in the parsed seasons/episodes lists
        season_match = item_season in parsed.get('seasons', [])
        episode_match = item_episode in parsed.get('episodes', [])

        # Check fallback episode if PTT episodes are empty
        if not parsed.get('episodes') and parsed.get('fallback_episode') == item_episode:
             episode_match = True

        # Match only if title, season, and episode all match
        match_result = title_match and season_match and episode_match
        #logging.debug(f"Episode match check: Title Match: {title_match} (Ratio: {title_ratio}), S: {season_match}, E: {episode_match} -> Result: {match_result}")
        return match_result

    @staticmethod
    def is_video_file(filename: str) -> bool:
        """Check if a file is a video file based on extension"""
        video_extensions = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.mov'}
        return any(filename.lower().endswith(ext) for ext in video_extensions)

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize a title for comparison by removing special characters and whitespace"""
        if not title:
            return ''
        # Remove special characters and normalize spaces
        normalized = title.lower()
        normalized = ''.join(c for c in normalized if c.isalnum() or c.isspace())
        normalized = ' '.join(normalized.split())  # Normalize whitespace
        return normalized

    @staticmethod
    def _is_acceptable_year_mismatch(item: Dict[str, Any], parsed: Dict[str, Any]) -> bool:
        """Check if year mismatch is acceptable (within 1 year)"""
        item_year = item.get('year')
        parsed_year = parsed.get('year')
        if not item_year or not parsed_year:
            return False # Changed to False if either year is missing for stricter check maybe? Or True? Let's keep original logic: True if one is missing.
        # Revert to original logic: If one year is missing, it's acceptable. Only compare if both exist.
        if item_year and parsed_year:
             return abs(int(item_year) - int(parsed_year)) <= 1
        return True # Acceptable if one or both years are missing

    def find_related_items(self, parsed_torrent_files: List[Dict[str, Any]], scraping_items: List[Dict[str, Any]], wanted_items: List[Dict[str, Any]], original_item: Dict[str, Any], xem_mapping: Optional[Dict[str, int]] = None, torrent_title: Optional[str] = None, claimed_file_paths: Optional[List[str]] = None) -> List[Tuple[Dict[str, Any], str]]:
        """
        Find items in the scraping and wanted queues that match pre-parsed files in the torrent.

        Args:
            parsed_torrent_files: List of dictionaries from _parse_file_info for files in the torrent.
            scraping_items: List of items currently in scraping state.
            wanted_items: List of items currently in wanted state.
            original_item: The original item being processed, used to match version/title.
            xem_mapping: Optional dictionary with 'season' from PTT of the torrent title, to enforce season-matching for packs.
            torrent_title: Optional torrent title string to parse for season information.
            claimed_file_paths: Paths already assigned to another item (e.g. the file the
                primary item matched), which must not be handed to a second episode.

        Returns:
            List of tuples, where each tuple contains (related_item_dict, matching_filepath_basename).
        """
        related_matches = []
        # One file may satisfy at most one episode. Without this, a pack of
        # bare-numbered files hands the SAME file to the same episode number in
        # every season -- one Dragon Ball Z file was claimed by S01E23 through
        # S09E23. Season labels legitimately disagree with the metadata provider
        # (a file marked S01E03 really can be the provider's S00E03), so the
        # season is not a safe discriminator; reuse of one file is.
        # Keyed on basename, which is the file identity used everywhere else here
        # (related_matches and filled_by_file both store basenames).
        claimed_basenames = {os.path.basename(p) for p in (claimed_file_paths or ())}
        # Plus anything a previous pass already handed out -- see
        # _basenames_already_in_use for why the in-memory set alone is not enough.
        # Only flat files (no directory component) are checked against the DB:
        # for those the basename IS the file identity. Foldered packs reuse
        # generic per-season basenames ('Season 1/01.mkv', 'Season 2/01.mkv'),
        # so a bare-basename DB claim would mark every file of a NEW season as
        # already owned by the old one and block the whole pack from sibling
        # filling. The in-memory set still prevents double-assignment within a
        # pass, which is where the Dragon Ball Z many-seasons collision arose.
        flat_basenames = [os.path.basename(pf['path']) for pf in parsed_torrent_files
                          if not os.path.dirname(pf.get('path') or '')]
        claimed_basenames |= self._basenames_already_in_use(
            flat_basenames,
            original_item.get('imdb_id'),
            exclude_item_id=original_item.get('id'),
        )
        original_version = original_item.get('version')
        # Ensure consistent title check (e.g., using series_title if available)
        original_title_to_check = original_item.get('series_title') or original_item.get('title')

        all_candidate_items = scraping_items + wanted_items
        processed_item_ids = set() # Prevent adding the same item ID twice

        logging.debug(f"Checking {len(all_candidate_items)} candidate items against {len(parsed_torrent_files)} parsed files.")

        # Allow multi-season matching if the user disables the restriction via settings.
        from utilities.settings import get_setting
        restrict_to_pack_season = get_setting('Matching', 'restrict_related_to_pack_season', False)

        # --- ENHANCED SEASON DETECTION ---
        # Determine the pack season from multiple sources in order of priority:
        pack_season = None
        
        # 1. First try XEM mapping (existing logic)
        if xem_mapping and 'season' in xem_mapping:
            pack_season = xem_mapping.get('season')
            logging.debug(f"Using XEM mapping for pack season: {pack_season}")
        
        # 2. If no XEM mapping, try parsing the torrent title directly
        if pack_season is None and torrent_title:
            try:
                from scraper.functions.ptt_parser import parse_with_ptt
                torrent_parsed = parse_with_ptt(torrent_title)
                torrent_seasons = torrent_parsed.get('seasons', [])
                
                if torrent_seasons:
                    # If multiple seasons, this is a multi-season pack
                    if len(torrent_seasons) > 1:
                        logging.info(f"Torrent title indicates multi-season pack: {torrent_seasons}")
                        # For multi-season packs, we might want to allow all seasons
                        # But for now, let's be conservative and only allow if user setting is disabled
                        if not restrict_to_pack_season:
                            pack_season = None  # Allow all seasons
                        else:
                            pack_season = torrent_seasons[0]  # Restrict to first season
                    else:
                        pack_season = torrent_seasons[0]
                        logging.info(f"Torrent title indicates single season pack: S{pack_season}")
            except Exception as e:
                logging.debug(f"Could not parse torrent title for season info: {e}")
        
        # 3. Fallback to original item's season if still no pack season
        if pack_season is None:
            pack_season = original_item.get('season') or original_item.get('season_number')
            if pack_season:
                logging.debug(f"Using original item season as pack season: {pack_season}")

        # Apply season restriction if we have a pack season and the setting is enabled
        if pack_season is not None and restrict_to_pack_season:
            logging.info(f"Torrent pack identified as Season {pack_season}. Related item matching will be restricted to this season (per setting).")

        # Determine relaxed matching based on original item (assuming related items follow same logic)
        genres = original_item.get('genres') or []
        if isinstance(genres, str):
            genres = [genres]
        is_anime = any('anime' in genre.lower() for genre in genres)
        file_collection_management = get_setting('File Management', 'file_collection_management')
        using_plex = file_collection_management == 'Plex'
        # Apply relaxed matching globally based on the original item context
        use_relaxed_matching_for_all = not using_plex and (is_anime or self.relaxed_matching)

        # Build indexes once for this batch to avoid O(Files × Items)
        indexes = self._build_parsed_file_indexes(parsed_torrent_files)
        by_season_episode = indexes['by_season_episode']
        by_episode_only = indexes['by_episode_only']
        f1_candidates = indexes['f1_candidates']

        for item in all_candidate_items:
            item_id = item.get('id')
            if not item_id or item_id in processed_item_ids:
                continue

            # Optionally skip items from other seasons for season packs
            if pack_season is not None and restrict_to_pack_season:
                item_season = item.get('season') or item.get('season_number')
                if item_season != pack_season:
                    continue
            
            # Check if this specific candidate item is anime
            candidate_genres = item.get('genres', [])
            if candidate_genres is None:
                candidate_genres = []
            elif isinstance(candidate_genres, str):
                candidate_genres = [candidate_genres]
            candidate_is_anime = any('anime' in g.lower() for g in candidate_genres)

            # --- Build per-candidate XEM mapping (season-offset only) ---
            candidate_xem_mapping = None
            if xem_mapping is not None:
                # Determine season delta based on original→scene mapping for the primary item
                original_item_season = original_item.get('season') or original_item.get('season_number')
                mapped_primary_season = xem_mapping.get('season')

                try:
                    if original_item_season is not None and mapped_primary_season is not None:
                        season_delta = int(mapped_primary_season) - int(original_item_season)

                        candidate_season = item.get('season') or item.get('season_number')
                        candidate_episode = item.get('episode') or item.get('episode_number')

                        if candidate_season is not None and candidate_episode is not None:
                            candidate_xem_mapping = {
                                'season': candidate_season + season_delta,
                                'episode': candidate_episode,  # assume episode number itself is unchanged
                            }
                except Exception as map_err:
                    logging.debug(f"Could not build candidate XEM mapping for item ID {item_id}: {map_err}")

            # Basic filtering for relevance
            item_title_to_check = item.get('series_title') or item.get('title')
            if (item.get('type') != 'episode' or
                item.get('version') != original_version or
                item_title_to_check != original_title_to_check):
                continue

            # --- Apply XEM mapping logic directly to the candidate item for matching ---
            # This part is complex as XEM was applied based on the *chosen scrape result* before.
            # We don't have that context easily here.
            # Simplification: Assume related items use their absolute S/E for matching for now.
            # A more robust solution would require passing XEM context differently.
            item_for_matching = item # Use the item directly

            # --- Find Match in Parsed Files ---
            found_match_for_this_item = False
            # Pre-select candidate files using indexes to drastically reduce comparisons
            candidate_files: List[Dict[str, Any]] = []
            seen_ids = set()

            # Formula 1 special case: use f1 keyword buckets
            item_title_for_f1_check = (item.get('series_title', '') or item.get('title', '')).lower()
            is_formula_1_item = ("formula 1" in item_title_for_f1_check) and ("drive to survive" not in item_title_for_f1_check)
            if is_formula_1_item:
                for key in ('session', 'qualifying', 'race'):
                    for pf in f1_candidates.get(key, []):
                        if id(pf) not in seen_ids:
                            seen_ids.add(id(pf)); candidate_files.append(pf)
            else:
                # Determine target season/episode (respect per-candidate XEM mapping if any)
                target_season = item.get('season') or item.get('season_number')
                target_episode = item.get('episode') or item.get('episode_number')

                if candidate_xem_mapping is not None:
                    try:
                        mapped_season = candidate_xem_mapping.get('season')
                        mapped_episode = candidate_xem_mapping.get('episode', target_episode)
                        if mapped_season is not None:
                            target_season = int(mapped_season)
                        if mapped_episode is not None:
                            target_episode = int(mapped_episode)
                    except Exception:
                        pass

                if target_episode is not None:
                    # Exact (season, episode)
                    for pf in by_season_episode.get((target_season, target_episode), []):
                        if id(pf) not in seen_ids:
                            seen_ids.add(id(pf)); candidate_files.append(pf)
                    # (None, episode)
                    for pf in by_season_episode.get((None, target_episode), []):
                        if id(pf) not in seen_ids:
                            seen_ids.add(id(pf)); candidate_files.append(pf)
                    # Episode-only. Speed-only index, no season authority --
                    # _check_match makes the season decision for every candidate.
                    for pf in by_episode_only.get(target_episode, []):
                        if id(pf) not in seen_ids:
                            seen_ids.add(id(pf)); candidate_files.append(pf)

                    # Anime: include absolute-episode candidates
                    if candidate_is_anime:
                        # Build a minimal item clone with mapped S/E for absolute computation
                        item_clone_for_abs = dict(item)
                        if target_season is not None:
                            item_clone_for_abs['season'] = target_season
                            item_clone_for_abs['season_number'] = target_season
                        if target_episode is not None:
                            item_clone_for_abs['episode'] = target_episode
                            item_clone_for_abs['episode_number'] = target_episode
                        abs_ep = self._compute_absolute_episode_for_item(item_clone_for_abs)
                        if abs_ep is not None:
                            for pf in by_episode_only.get(abs_ep, []):
                                if id(pf) not in seen_ids:
                                    seen_ids.add(id(pf)); candidate_files.append(pf)
                            for pf in by_season_episode.get((1, abs_ep), []):
                                if id(pf) not in seen_ids:
                                    seen_ids.add(id(pf)); candidate_files.append(pf)
                else:
                    # Fallback: if we somehow lack episode number, consider all files (rare)
                    candidate_files = parsed_torrent_files

            for parsed_file_info in candidate_files:
                # Always skip files tagged as anime special content
                if parsed_file_info.get('parsed_info', {}).get('is_anime_special_content', False):
                    logging.debug(f"Skipping anime special file '{parsed_file_info['path']}' for related item matching.")
                    continue

                # Already handed to another episode (or to the primary item).
                if os.path.basename(parsed_file_info['path']) in claimed_basenames:
                    continue

                # Pass per-candidate mapping (if available) so scene numbering is considered correctly
                if self._check_match(
                    parsed_file_info,
                    item_for_matching,
                    use_relaxed_matching_for_all,
                    xem_mapping=candidate_xem_mapping,
                ):
                    logging.info(f"Found related item ID {item_id} (State: {item.get('state', 'Unknown')}) matching file '{parsed_file_info['path']}'")
                    # Store the item and the *basename* of the matched file path
                    related_matches.append((item, os.path.basename(parsed_file_info['path'])))
                    claimed_basenames.add(os.path.basename(parsed_file_info['path']))
                    processed_item_ids.add(item_id) # Mark as processed
                    found_match_for_this_item = True
                    break # Move to the next candidate item once a match is found for this one

            # Optional: Debug log for non-matches
            # if not found_match_for_this_item:
            #     logging.debug(f"No file match found for candidate item ID {item_id}")

        logging.debug(f"Found {len(related_matches)} related items matching files in total.")
        return related_matches
