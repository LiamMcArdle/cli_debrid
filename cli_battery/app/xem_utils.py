import requests
import json
import time
from typing import Optional, List, Dict, Any
from .logger_config import logger

XEM_API_URL = "https://thexem.info/map/all"
XEM_NAMES_URL = "https://thexem.info/map/names"
# TheXEM answers 403 to the app's own User-Agent string (measured 2026-09-03:
# 160 of 160 mapping requests in one hour), which had silently disabled XEM
# for every show. It answers 200 to an ordinary browser UA -- the same one
# scraper/torrentio.py already sends.
_XEM_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
}


def fetch_xem_names(tvdb_id: int) -> Optional[Dict[str, List[str]]]:
    """Per-season scene names from TheXEM: {season_number: [names]}.

    This is where the release groups' own names for a season live -- for
    Bleach S17 both 'Thousand-Year Blood War' and 'Sennen Kessen-hen'. The
    provider's season title covers the first; nothing else covers the second.
    Returns {} when XEM has no show under the id (definitive), None when no
    answer was obtained. Season 0 is dropped: its names are never distinctive.
    """
    global _xem_blocked_until
    if not tvdb_id:
        return None
    if time.time() < _xem_blocked_until:
        return None
    try:
        response = requests.get(XEM_NAMES_URL, params={'id': tvdb_id, 'origin': 'tvdb'},
                                headers=_XEM_HEADERS, timeout=15)
        if response.status_code == 403:
            _xem_blocked_until = time.time() + _XEM_COOLDOWN_SECONDS
            logger.warning(f"TheXEM returned 403 for names of TVDB ID {tvdb_id}. Pausing XEM requests for {_XEM_COOLDOWN_SECONDS}s.")
            return None
        response.raise_for_status()
        data = response.json()
        if data.get("result") != "success":
            if "no show with the" in str(data.get("message", "")):
                return {}
            return None
        names: Dict[str, List[str]] = {}
        for season, by_country in (data.get("data") or {}).items():
            try:
                if int(season) < 1:
                    continue
            except (TypeError, ValueError):
                continue
            collected = []
            values = by_country.values() if isinstance(by_country, dict) else [by_country]
            for group in values:
                for name in (group or []):
                    if isinstance(name, str) and name.strip() and name not in collected:
                        collected.append(name.strip())
            if collected:
                names[str(int(season))] = collected
        return names
    except Exception as e:
        logger.debug(f"Error requesting XEM names for TVDB ID {tvdb_id}: {e}")
        return None

# Simple circuit breaker: if XEM returns 403 (IP/rate blocked), pause all
# requests for _XEM_COOLDOWN_SECONDS to avoid hammering the server.
_xem_blocked_until: float = 0.0
_XEM_COOLDOWN_SECONDS = 300  # 5-minute cooldown after a 403

def fetch_xem_mapping(tvdb_id: int) -> Optional[List[Dict[str, Any]]]:
    """
    Fetches the episode numbering mapping for a given TVDB ID from TheXEM.

    Args:
        tvdb_id: The TVDB ID of the show.

    Returns:
        A list of mapping dictionaries if successful; an EMPTY list when TheXEM
        answered definitively that it has no show under this id; None when no
        answer was obtained (cooldown, 403, timeout, transport or parse error).
        Callers cache the empty list for a long time and the None for a short
        one -- most shows have no XEM entry, and re-asking about them every
        few hours was one HTTP round trip per show per scrape cycle.
        Each dictionary in the list typically contains keys like 'scene', 'tvdb', etc.,
        each mapping to another dictionary with 'season', 'episode', 'absolute'.
    """
    global _xem_blocked_until
    if not tvdb_id:
        logger.warning("fetch_xem_mapping called with no TVDB ID.")
        return None

    # Circuit breaker: skip if XEM recently returned 403
    if time.time() < _xem_blocked_until:
        remaining = int(_xem_blocked_until - time.time())
        logger.debug(f"XEM circuit breaker active, skipping request for TVDB ID {tvdb_id} ({remaining}s remaining).")
        return None

    params = {'id': tvdb_id, 'origin': 'tvdb'}
    url = f"{XEM_API_URL}"
    headers = _XEM_HEADERS

    try:
        logger.info(f"Querying TheXEM for TVDB ID {tvdb_id}...")
        response = requests.get(url, params=params, headers=headers, timeout=15)
        if response.status_code == 403:
            _xem_blocked_until = time.time() + _XEM_COOLDOWN_SECONDS
            logger.warning(f"TheXEM returned 403 for TVDB ID {tvdb_id}. Pausing XEM requests for {_XEM_COOLDOWN_SECONDS}s.")
            return None
        response.raise_for_status()  # Raise an exception for other bad status codes

        data = response.json()

        if data.get("result") == "success":
            logger.info(f"Successfully retrieved XEM mapping for TVDB ID {tvdb_id}.")
            mapping = data.get("data") # This should be the list of mappings
            return mapping if isinstance(mapping, list) else []
        else:
            message = data.get("message", "Unknown reason")
            # Don't log an error if the show simply isn't found, just info.
            if "no show with the" in message:
                 logger.info(f"No mapping found on TheXEM for TVDB ID {tvdb_id}: {message}")
                 return []
            logger.error(f"Failed to retrieve XEM mapping for TVDB ID {tvdb_id}. Result: {data.get('result')}, Message: {message}")
            return None

    except requests.exceptions.Timeout:
        logger.error(f"Timeout while requesting XEM mapping for TVDB ID {tvdb_id}.")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error requesting XEM mapping for TVDB ID {tvdb_id}: {e}")
        return None
    except json.JSONDecodeError:
        logger.error(f"Error decoding JSON response from TheXEM for TVDB ID {tvdb_id}.")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred in fetch_xem_mapping for TVDB ID {tvdb_id}: {e}", exc_info=True)
        return None