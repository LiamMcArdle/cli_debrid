from routes.api_tracker import api
from scraper.scrape_status import ScraperUnavailable
import hashlib
import logging
import threading
import time
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from utilities.settings import get_setting
from urllib.parse import urlencode, quote_plus
import re
import json
from scraper.functions.common import trim_magnet

# Anime is published by absolute episode number ("One Piece 1069"), not SxxExx,
# so the structured tvsearch below finds nothing for most shows. These are the
# formats worth spending a keyword search on. 'regular' (S04E01) is already what
# the tvsearch asks for; 'combined' (S04E60) is not a convention any release
# group uses -- it exists only so filter_results can match a stray one; 'batch'
# is an empty marker rather than a search term; 'season' (S02) duplicates the
# title query. Measured 2026-09-08 over the 12 shows that were blacklisting:
# the SxxExx query yielded 30 usable results from 817 raw and NOTHING AT ALL for
# 6 of the 12, while these keyword queries yielded 191 usable from 438 raw.
_SKIP_ANIME_FORMATS = frozenset({'regular', 'combined', 'batch', 'season'})
_MAX_ANIME_KEYWORD_QUERIES = 2
# The useful anime responses measured 1-140 results; 1000 is for index dumps.
_ANIME_KEYWORD_LIMIT = 200


# Query result cache — reduces API hits for duplicate NZB searches within TTL window
_NZB_CACHE: Dict[str, tuple] = {}   # key -> (timestamp, results)
_NZB_CACHE_LOCK = threading.Lock()
_NZB_CACHE_TTL = 600  # 10 minutes
# A TTL enforced only on read expires nothing here. The key is the query, and a
# queue working through a backlog asks a different question every time, so
# almost every entry is written once and never looked up again -- _cache_get
# never runs for it, so its delete never runs either. Each entry holds a
# ~500-element result list whose dicts carry Prowlarr's recursive category
# tree, which is how the process reached 6 GB in 17 hours. The cache has to
# sweep on write, and be bounded, or it outlives its own TTL forever.
_NZB_CACHE_MAX = 512


def _cache_key(endpoint: str, params: dict) -> str:
    stable = f"{endpoint}|{sorted(params.items())}"
    return hashlib.sha256(stable.encode()).hexdigest()


def _cache_get(key: str) -> Optional[List]:
    with _NZB_CACHE_LOCK:
        entry = _NZB_CACHE.get(key)
        if entry and (time.monotonic() - entry[0]) < _NZB_CACHE_TTL:
            return entry[1]
        if entry:
            del _NZB_CACHE[key]
    return None


def _cache_set(key: str, results: List) -> None:
    with _NZB_CACHE_LOCK:
        now = time.monotonic()
        if len(_NZB_CACHE) >= _NZB_CACHE_MAX:
            for stale in [k for k, e in _NZB_CACHE.items()
                          if now - e[0] >= _NZB_CACHE_TTL]:
                del _NZB_CACHE[stale]
            # Still full means the entries are genuinely live, not stale, so
            # age them out oldest-first rather than letting the cap be advisory.
            if len(_NZB_CACHE) >= _NZB_CACHE_MAX:
                oldest = sorted(_NZB_CACHE.items(), key=lambda kv: kv[1][0])
                for k, _ in oldest[:max(1, _NZB_CACHE_MAX // 8)]:
                    del _NZB_CACHE[k]
        _NZB_CACHE[key] = (now, results)


def _anime_keyword_queries(clean_title: str, episode_formats: Dict[str, str]) -> List[str]:
    """Absolute-numbering keyword searches for an anime episode.

    episode_formats is already built for every anime scrape by
    scraper.convert_anime_episode_format; it was simply never offered to
    anything but Nyaa. Values are deduplicated because the XEM 'orig_' variants
    frequently repeat a pattern already present.
    """
    queries: List[str] = []
    for key, pattern in (episode_formats or {}).items():
        base = key[5:] if key.startswith('orig_') else key
        if base in _SKIP_ANIME_FORMATS:
            continue
        pattern = str(pattern or '').strip()
        if not pattern:
            continue
        query = f"{clean_title} {pattern}"
        if query not in queries:
            queries.append(query)
        if len(queries) >= _MAX_ANIME_KEYWORD_QUERIES:
            break
    return queries


def _build_prowlarr_params_list(
    title: str,
    year: int,
    content_type: str,
    imdb_id: Optional[str],
    tmdb_id: Optional[str],
    season: Optional[int],
    episode: Optional[int],
    multi: bool,
    tags_setting: str,
    episode_formats: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Always build both ID-based and title-based queries to run simultaneously."""
    base = {'limit': 1000, 'offset': 0}
    if tags_setting:
        try:
            ids = [int(t.strip()) for t in tags_setting.split(',') if t.strip().isdigit()]
            if ids:
                base['indexerIds'] = ids
        except ValueError:
            pass

    params_list = []
    clean_title = rename_special_characters(title)

    if content_type.lower() == 'movie':
        q = f"{clean_title} {year or ''}".strip()
        # 1. ID search. The title rides along: an indexer that implements the ID
        # search still uses the ID, and one that does not now gets a real search
        # term instead of a request to dump its most recent `limit` entries.
        if imdb_id or tmdb_id:
            p = {**base, 'type': 'movie', 'query': q}
            if imdb_id:
                p['imdbId'] = imdb_id.replace('tt', '')
            elif tmdb_id:
                p['tmdbId'] = tmdb_id
            params_list.append(p)
        # 2. Title search
        params_list.append({**base, 'type': 'movie', 'query': q})

    elif content_type.lower() == 'episode':
        season_ep: Dict[str, Any] = {}
        if season is not None:
            season_ep['season'] = season
            if episode is not None and not multi:
                season_ep['episode'] = episode
        # 1. ID search, structured season/ep. The title rides along for the same
        # reason as the movie branch above.
        if imdb_id or tmdb_id:
            p = {**base, 'type': 'tvsearch', 'query': clean_title, **season_ep}
            if imdb_id:
                p['imdbId'] = imdb_id.replace('tt', '')
            elif tmdb_id:
                p['tmdbId'] = tmdb_id
            params_list.append(p)
        # 2. Title text search
        q_parts = [clean_title]
        if season is not None:
            if episode is not None and not multi:
                q_parts.append(f'S{season:02d}E{episode:02d}')
            else:
                q_parts.append(f'S{season:02d}')
        params_list.append({**base, 'type': 'tvsearch', 'query': ' '.join(q_parts), **season_ep})
        # 3. Anime absolute numbering. A plain keyword search, because no
        # Newznab category expresses "episode 1069 of a show with 23 seasons".
        for anime_q in _anime_keyword_queries(clean_title, episode_formats):
            params_list.append({**base, 'limit': _ANIME_KEYWORD_LIMIT,
                                'type': 'search', 'query': anime_q})

    else:
        q = f"{clean_title} {year or ''}".strip()
        params_list.append({**base, 'type': 'search', 'query': q})

    # An empty search term asks any indexer that does not implement the ID
    # search to return its most recent `limit` entries instead. Measured
    # 2026-09-08: one such query returned the same 557-result index page for all
    # 12 shows tested -- the same "Limitless"/XXX/soap-opera dump -- with ~zero
    # relevant hits, and it accounted for roughly 74% of all results Prowlarr
    # returned. Dropping the query is not enough; nothing may reintroduce one.
    return [p for p in params_list if str(p.get('query') or '').strip()]


def scrape_prowlarr_instance(
    instance: str,
    settings: Dict[str, Any],
    imdb_id: Optional[str],
    title: str,
    year: int,
    content_type: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    multi: bool = False,
    tmdb_id: Optional[str] = None,
    episode_formats: Optional[Dict[str, str]] = None
) -> List[Dict[str, Any]]:
    logging.info(f"Scraping Prowlarr instance: {instance} for '{title}' ({year})")
    prowlarr_url = settings.get('url', '').rstrip('/')
    # The settings schema calls this field 'api' (as does jackett.py), so an
    # instance configured through the UI only ever has that key. Reading
    # 'api_key' alone made every such instance log "missing API key" and return
    # nothing, silently and forever.
    prowlarr_api_key = settings.get('api') or settings.get('api_key', '')

    if not prowlarr_url or not prowlarr_api_key:
        logging.error(f"Prowlarr instance '{instance}' is missing URL or API key.")
        return []

    tags_setting = settings.get('tags', '')
    headers = {'X-Api-Key': prowlarr_api_key, 'accept': 'application/json'}
    search_endpoint = f"{prowlarr_url}/api/v1/search"
    # scraper_timeout's own setting description says "0 to disable" — requests
    # treats timeout=None (not 0) as no timeout, so 0/falsy must map to None.
    timeout = get_setting('Scraping', 'scraper_timeout', 30) or None
    seeders_only = get_setting('Scraping', 'prowlarr_seeders_only', get_setting('Scraping', 'jackett_seeders_only', True))

    params_list = _build_prowlarr_params_list(
        title, year, content_type, imdb_id, tmdb_id, season, episode, multi, tags_setting,
        episode_formats=episode_formats
    )

    def _fetch(query_params):
        ck = _cache_key(search_endpoint, query_params)
        cached = _cache_get(ck)
        if cached is not None:
            logging.info(f"Prowlarr '{instance}' cache hit for {query_params.get('type')} query")
            return cached
        try:
            logging.debug(f"Prowlarr '{instance}' query: {query_params}")
            response = api.get(search_endpoint, headers=headers, params=query_params, timeout=timeout)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    results = parse_prowlarr_results(data, instance, seeders_only)
                    if results:
                        _cache_set(ck, results)
                    return results
                logging.error(f"Prowlarr '{instance}' unexpected response type: {type(data)}")
            elif response.status_code in (429, 502, 503, 504) or response.status_code >= 500:
                raise ScraperUnavailable(
                    f"prowlarr '{instance}' HTTP {response.status_code}"
                )
            else:
                logging.error(f"Prowlarr '{instance}' HTTP {response.status_code}: {response.text[:300]}")
        except ScraperUnavailable:
            raise
        except api.exceptions.Timeout:
            logging.error(f"Prowlarr '{instance}' timed out")
            raise ScraperUnavailable(f"prowlarr '{instance}' timed out")
        except api.exceptions.RequestException as e:
            # Connection refused, DNS failure, TLS error: Prowlarr is down, not
            # empty. Returning [] here would tell the retry ladder the indexers
            # genuinely had nothing and cost the item a rung.
            logging.error(f"Prowlarr '{instance}' unreachable: {e}")
            raise ScraperUnavailable(f"prowlarr '{instance}' unreachable: {e}")
        except Exception as e:
            logging.error(f"Prowlarr '{instance}' error: {e}", exc_info=True)
        return []

    # Run all queries in parallel. Only report the instance as unavailable if
    # EVERY query failed to complete — one query 503ing while the other returns
    # results is a partial answer, not an outage, and discarding the good half
    # would be worse than the bug this guards against.
    all_instance_results: List[Dict[str, Any]] = []
    unavailable_errors: List[str] = []
    if len(params_list) == 1:
        all_instance_results = _fetch(params_list[0])
    else:
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = [ex.submit(_fetch, p) for p in params_list]
            for f in as_completed(futures):
                try:
                    all_instance_results.extend(f.result())
                except ScraperUnavailable as e:
                    unavailable_errors.append(str(e))
        if unavailable_errors and len(unavailable_errors) == len(params_list):
            raise ScraperUnavailable('; '.join(unavailable_errors))

    seen_keys = set()
    unique_results = []
    for result in all_instance_results:
        unique_key = result.get('parsed_info', {}).get('guid') or result.get('magnet')

        if unique_key and unique_key not in seen_keys:
            seen_keys.add(unique_key)
            unique_results.append(result)
        elif not unique_key:
            logging.warning(f"Prowlarr result for '{result.get('title')}' has no GUID or magnet for deduplication. Adding it anyway.")
            unique_results.append(result)

    logging.info(f"Found {len(unique_results)} unique results from Prowlarr instance {instance} for '{title}' ({len(all_instance_results)} total before dedup)")
    return unique_results

def parse_prowlarr_results(data: List[Dict[str, Any]], ins_name: str, seeders_only: bool) -> List[Dict[str, Any]]:
    results = []
    if not isinstance(data, list):
        logging.error(f"Prowlarr parsing error: Expected a list, got {type(data)}")
        return results

    filtered_no_link = 0
    filtered_no_seeders = 0

    logging.debug(f"Parsing {len(data)} items from Prowlarr instance {ins_name}") # Added count log
    for idx, item in enumerate(data): # Added index for logging

        title = item.get('title', 'N/A')
        
        magnet_url = item.get('magnetUrl')
        download_url = item.get('downloadUrl')
        info_hash = item.get('infoHash', '').lower()

        primary_link = None
        is_torrent_url = False

        guid = item.get('guid')
        
        item_protocol = item.get('protocol', 'torrent').lower()
        is_nzb = (item_protocol == 'nzb')

        if magnet_url and magnet_url.startswith('magnet:'):
            primary_link = trim_magnet(magnet_url)
        elif download_url:
            primary_link = download_url
            is_torrent_url = not is_nzb
        # Check if the guid field contains a magnet link
        elif guid and guid.startswith('magnet:'):
            primary_link = trim_magnet(guid)
            logging.debug(f"Using magnet link from guid field for '{title}' from {ins_name}")
        else:
            filtered_no_link += 1
            item_debug = {
                'title': title,
                'indexer': item.get('indexer', 'Unknown'),
                'guid': guid,
                'protocol': item_protocol,
                'categories': item.get('categories', []),
                'has_magnet': magnet_url is not None,
                'has_download': download_url is not None
            }
            logging.debug(f"Skipping Prowlarr result '{title}' from {ins_name} - No magnetUrl or downloadUrl found. Item details: {json.dumps(item_debug, indent=2)}")
            continue

        seeders = item.get('seeders', 0)
        # Don't filter NZB results by seeders — usenet doesn't have seeders
        if seeders_only and seeders == 0 and not is_nzb:
            filtered_no_seeders += 1
            continue
            
        size_bytes = item.get('size', 0)
        size_gb = round(size_bytes / (1024 * 1024 * 1024), 2) if size_bytes else 0.0

        indexer_name = item.get('indexer', 'Unknown Indexer')
        source_name = f"{ins_name} - {indexer_name}"

        if not info_hash and magnet_url and magnet_url.startswith('magnet:'):
            match = re.search(r'urn:btih:([a-fA-F0-9]{40})', magnet_url, re.IGNORECASE)
            if match:
                info_hash = match.group(1).lower()
        
        if not title or not primary_link:
            logging.warning(f"Skipping Prowlarr item due to missing title or link: {item}")
            continue

        parsed_info = {
            'guid': item.get('guid'),
            'indexer_id_prowlarr': item.get('indexerId'),
            'protocol': item_protocol,
            'publish_date': item.get('publishDate'),
            'leechers': item.get('leechers'),
            'peers': item.get('peers', seeders + item.get('leechers', 0)),
            'grabs': item.get('grabs') or item.get('snatches'),
            'categories_prowlarr': item.get('categories', []),
            'imdb_id_prowlarr': item.get('imdbId'),
            'tmdb_id_prowlarr': item.get('tmdbId'),
            'tvdb_id_prowlarr': item.get('tvdbId'),
            'indexer_raw_name': item.get('indexer'),
            'rejections': item.get('rejections') 
        }

        result_dict = {
            'title': title,
            'size': size_gb,
            'source': source_name,
            'seeders': seeders,
            'hash': info_hash,
            'parsed_info': parsed_info,
            'magnet': None,
            'torrent_url': None,
            'magnet_link': None,
            'protocol': item_protocol,
            'nzb_url': primary_link if is_nzb else None,
        }

        if is_nzb:
            # NZB result — primary_link is the NZB download URL
            result_dict['magnet'] = primary_link
        else:
            result_dict['magnet'] = primary_link
            if is_torrent_url:
                result_dict['torrent_url'] = primary_link
                if info_hash:
                    constructed_magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote_plus(str(title))}"
                    result_dict['magnet_link'] = constructed_magnet
            else:
                result_dict['magnet_link'] = primary_link

        results.append(result_dict)

    if filtered_no_link > 0 or filtered_no_seeders > 0:
        logging.debug(f"Prowlarr parsing summary for {ins_name}: Total items: {len(data)}, Parsed: {len(results)}, Filtered (no link): {filtered_no_link}, Filtered (no seeders): {filtered_no_seeders}")

    return results

def rename_special_characters(text: str) -> str:
    '''
    replacements = [
        ("&", ""), ("\u00fc", "ue"), ("\u00e4", "ae"), ("\u00e2", "a"),
        ("\u00e1", "a"), ("\u00e0", "a"), ("\u00f6", "oe"), ("\u00f4", "o"),
        ("\u00e8", "e"), (":", ""), ("(", ""), (")", ""), ("`", ""),
        (",", ""), ("!", ""), ("?", ""), (" - ", " "), ("'", ""),
        ("*", ""), (".", " "),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    text = text.replace("'", "")
    '''
    return text
