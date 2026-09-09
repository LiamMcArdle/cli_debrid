"""Wake the anime episodes whose absolute number was being read wrong.

Until the identity resolver landed, the absolute episode number an anime
search asked for was the metadata battery's value at the positional
(season, episode) key. Where the battery and the library hold different
season layouts for the same show, that key names a different episode and
every search went out with a number no release carries; those items walked
the retry ladder and were blacklisted for a reason that was never theirs.

This script lists (and with --apply, wakes) the pending anime episode rows
whose resolved absolute differs from the positional one, or which are now
known by a second number they never asked for. It is deliberately manual and
batched: several hundred items entering Wanted at once is a Prowlarr flood.

    python utilities/wake_remapped_absolutes.py            # dry run, full list
    python utilities/wake_remapped_absolutes.py --apply    # wake the first 100
    python utilities/wake_remapped_absolutes.py --apply --limit 50

Woken rows leave the eligible set, so repeated --apply runs walk the list.
"""
import argparse
import json
import logging
import os
import sys

# Runnable from anywhere: put the project root ahead of utilities/ on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

import database  # noqa: F401  (circular import: must be imported first)
from database.database_reading import get_all_media_items, get_all_season_episode_counts
from scraper.scraper import load_battery_episode_rows, _arithmetic_absolute
from scraper.functions.season_resolution import (
    absolute_episode_from_identity, ABS_AMBIGUOUS,
)

# Dormant and Sleeping rows are still cycling. A Blacklisted row is ours to
# wake when the machine put it there: the retry ladder ('exhausted'), or the
# pre-ladder logic that blacklisted by age and took whole seasons out on one
# dead coordinate -- those rows carry no failure record at all. A person's
# blacklist is ghostlisted, and move_to_wanted refuses it anyway.
WAKE_STATES = ('Dormant', 'Sleeping', 'Blacklisted')
BLACKLIST_STAGES = (None, 'exhausted', 'provider_blocked')


def _failure_stage(item: Dict[str, Any]) -> Optional[str]:
    blob = item.get('last_scrape_failure')
    if not blob:
        return None
    try:
        return json.loads(blob).get('stage')
    except (TypeError, ValueError, AttributeError):
        return None


def _is_anime(item: Dict[str, Any]) -> bool:
    genres = item.get('genres') or ''
    return 'anime' in str(genres).lower()


def _eligible(item: Dict[str, Any]) -> bool:
    if item.get('type') != 'episode' or not _is_anime(item):
        return False
    if item.get('ghostlisted') == 1:
        return False
    state = item.get('state')
    if state in ('Dormant', 'Sleeping'):
        return True
    return state == 'Blacklisted' and _failure_stage(item) in BLACKLIST_STAGES


def find_remapped(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rows whose search number changes under the identity resolver."""
    by_show: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        if _eligible(item):
            by_show[item.get('imdb_id') or ''].append(item)

    found: List[Dict[str, Any]] = []
    for imdb_id, show_items in by_show.items():
        if not imdb_id:
            continue
        rows = load_battery_episode_rows(imdb_id)
        if not rows:
            continue
        counts: Dict[int, int] = {}
        tmdb_id = show_items[0].get('tmdb_id')
        if tmdb_id:
            try:
                counts = get_all_season_episode_counts(tmdb_id) or {}
            except Exception as exc:  # pragma: no cover - defensive
                logging.debug(f"No season counts for {tmdb_id}: {exc}")
        for item in show_items:
            season, episode = item.get('season_number'), item.get('episode_number')
            if season is None or episode is None:
                continue
            positional = next((r.absolute_episode for r in rows
                               if r.season_number == season and r.episode_number == episode), None)
            absolute, verdict, candidates = absolute_episode_from_identity(
                rows, season, episode, item.get('episode_title'), item.get('release_date'),
                _arithmetic_absolute(season, episode, counts))
            changed = absolute is not None and positional is not None and absolute != positional
            gains_alt = verdict == ABS_AMBIGUOUS and any(c != positional for c in candidates)
            if changed or gains_alt:
                found.append({
                    'item': item, 'positional': positional, 'absolute': absolute,
                    'verdict': verdict, 'candidates': candidates,
                })
    found.sort(key=lambda r: (r['item'].get('title') or '', r['item'].get('season_number') or 0,
                              r['item'].get('episode_number') or 0, r['item'].get('version') or ''))
    return found


def _describe(record: Dict[str, Any]) -> str:
    item = record['item']
    number = f"{record['positional']} -> {record['absolute']}"
    if record['candidates']:
        number += f" {tuple(record['candidates'])}"
    return (f"{item.get('id'):>8}  {item.get('state'):<11} {item.get('title')} "
            f"S{item.get('season_number'):02d}E{item.get('episode_number'):02d} "
            f"[{item.get('version')}]  {number}  {record['verdict']}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true', help='wake the rows (default: list only)')
    parser.add_argument('--limit', type=int, default=100, help='rows to wake per --apply run (default 100)')
    parser.add_argument('--show', help='only rows whose show title contains this text')
    args = parser.parse_args(argv)

    items = get_all_media_items(state=list(WAKE_STATES), media_type='episode')
    records = find_remapped(items)
    if args.show:
        needle = args.show.lower()
        records = [r for r in records if needle in (r['item'].get('title') or '').lower()]

    print(f"{len(records)} pending anime episode row(s) resolve to a different absolute number")
    for record in records:
        print(_describe(record))
    if not args.apply or not records:
        if records and not args.apply:
            print(f"\nDry run. Re-run with --apply to wake the first {args.limit}.")
        return 0

    from queues.queue_manager import QueueManager
    manager = QueueManager()
    woken = 0
    for record in records[:args.limit]:
        item = record['item']
        try:
            manager.move_to_wanted(item, item['state'])
            woken += 1
        except Exception as exc:
            logging.error(f"Could not wake item {item.get('id')}: {exc}")
    print(f"\nWoke {woken} of {min(args.limit, len(records))} row(s); {len(records) - woken} remain.")
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    sys.exit(main())
