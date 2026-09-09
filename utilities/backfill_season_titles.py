"""Fill in season names for shows the active metadata source left unnamed.

TVDB's season list carries no names for most shows, so the battery held
season names for 74 of 1,569 shows (all from TheXEM) and none for Pokemon.
The show refresh now supplements them from Trakt; this front-loads that for
every show already in the battery instead of waiting a day for each show's
refresh to come round. One Trakt request per show.

    python utilities/backfill_season_titles.py                # dry run
    python utilities/backfill_season_titles.py --apply        # write rows
    python utilities/backfill_season_titles.py --apply --show Pokemon
"""
import argparse
import logging
import os
import sys
import time
import unicodedata
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: F401  (circular import: must be imported first)
from cli_battery.app import direct_api
from cli_battery.app.database import Item, Metadata
from cli_battery.app.direct_api import SEASON_TITLES_KEY, _merge_season_titles_row, _trakt_season_titles


def _fold(text: Optional[str]) -> str:
    """Accent-insensitive, case-insensitive: 'Pokemon' finds 'Pokémon'."""
    decomposed = unicodedata.normalize('NFKD', text or '')
    return ''.join(c for c in decomposed if not unicodedata.combining(c)).lower()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true', help='write the rows (default: list only)')
    parser.add_argument('--show', help='only shows whose title contains this text')
    parser.add_argument('--pause', type=float, default=0.25, help='seconds between Trakt requests')
    args = parser.parse_args(argv)

    with direct_api.managed_session() as session:
        shows = session.query(Item).filter_by(type='show').order_by(Item.title).all()
        todo = []
        for show in shows:
            if not show.imdb_id:
                continue
            if args.show and _fold(args.show) not in _fold(show.title):
                continue
            row = session.query(Metadata).filter_by(item_id=show.id, key=SEASON_TITLES_KEY).first()
            sources = set(filter(None, (row.provider or '').split(','))) if row else set()
            if sources & {'provider', 'trakt'}:
                continue
            todo.append((show.id, show.imdb_id, show.title, row))
        print(f"{len(todo)} show(s) without provider or Trakt season names")
        if not args.apply:
            for _, imdb_id, title, _ in todo[:40]:
                print(f"  {imdb_id}  {title}")
            if len(todo) > 40:
                print(f"  ... {len(todo) - 40} more")
            print("\nDry run. Re-run with --apply to fetch and write.")
            return 0
        written = 0
        for index, (item_id, imdb_id, title, row) in enumerate(todo, 1):
            titles = _trakt_season_titles(imdb_id)
            if titles:
                _merge_season_titles_row(session, item_id, row, titles, source='trakt')
                session.commit()
                written += 1
                print(f"[{index}/{len(todo)}] {title}: {len(titles)} named season(s)")
            else:
                print(f"[{index}/{len(todo)}] {title}: none")
            time.sleep(args.pause)
        print(f"\nWrote season names for {written} of {len(todo)} show(s).")
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    sys.exit(main())
