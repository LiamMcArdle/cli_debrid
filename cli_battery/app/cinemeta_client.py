"""Cinemeta episode-coordinate lookups.

Cinemeta is the keyless metadata index that Stremio addons -- including
Torrentio -- are keyed on, so the (season, episode) coordinate it publishes for
a given TVDB episode id IS the coordinate those addons will answer to.

This matters because the library's own numbering comes from Trakt (TVDB and
TMDB API keys are unset, so cli_battery falls back to Trakt for everything) and
Trakt records some shows -- most anime -- as a single absolute-numbered season.
Asking Torrentio for ``tt12343534:1:25`` returns nothing, because upstream that
episode is ``tt12343534:2:1``.

The join is exact: cli_battery stores a ``tvdb_id`` on each episode row, and
Cinemeta publishes the same ``tvdb_id`` against its own season/episode pair.
No fuzzy title matching is involved.
"""

import logging
from typing import Dict, Optional, Tuple

import requests

CINEMETA_SERIES_URL = "https://v3-cinemeta.strem.io/meta/series/{imdb_id}.json"
REQUEST_TIMEOUT = (5, 10)
_HEADERS = {'User-Agent': 'cli_debrid/cinemeta-coordinate-resolver'}


def fetch_cinemeta_episode_map(imdb_id: str) -> Optional[Dict[str, Tuple[int, int]]]:
    """Return ``{tvdb_id: (season, episode)}`` for a series, or None on failure.

    Returns None -- never an empty dict -- when the request could not be
    completed, so the caller can distinguish 'this show has no data' from 'we
    could not ask'. Persisting a failure as an empty value with no expiry is
    exactly how the XEM integration became permanently dead.
    """
    if not imdb_id:
        return None
    url = CINEMETA_SERIES_URL.format(imdb_id=imdb_id)
    try:
        response = requests.get(url, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        videos = (response.json().get('meta') or {}).get('videos') or []
    except Exception as e:
        logging.warning(f"Cinemeta fetch failed for {imdb_id}: {e}")
        return None

    mapping = {}
    for video in videos:
        tvdb_id = video.get('tvdb_id')
        season = video.get('season')
        episode = video.get('episode')
        if tvdb_id is None or season is None or episode is None:
            continue
        try:
            mapping[str(tvdb_id)] = (int(season), int(episode))
        except (TypeError, ValueError):
            continue
    return mapping
