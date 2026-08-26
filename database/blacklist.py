from .core import get_db_connection
import logging
from typing import List
from datetime import datetime

def get_blacklisted_items():
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM media_items WHERE state = "Blacklisted"')
        items = cursor.fetchall()
        return [dict(item) for item in items]
    except Exception as e:
        logging.error(f"Error retrieving blacklisted items: {str(e)}")
        return []
    finally:
        conn.close()

def remove_from_blacklist(item_ids: List[int]):
    """Return blacklisted items to Wanted, resetting their retry ladder.

    The WHERE clause is deliberately self-defending rather than trusting the
    caller's id list. Ghostlisted rows are content the user deleted on purpose
    and must never come back, and rows whose imdb_id or tmdb_id is in
    manual_blacklist.json would simply be re-blacklisted by
    WantedQueue.move_blacklisted_items on the next pass -- an oscillation, plus
    a notification each cycle.
    """
    from database.manual_blacklist import is_blacklisted as is_manually_blacklisted

    conn = get_db_connection()
    restored = 0
    skipped = 0
    try:
        for item_id in item_ids:
            row = conn.execute(
                "SELECT imdb_id, tmdb_id, season_number FROM media_items "
                "WHERE id = ? AND state = 'Blacklisted' "
                "AND (ghostlisted IS NULL OR ghostlisted = 0)",
                (item_id,)
            ).fetchone()
            if row is None:
                skipped += 1
                continue

            season = row['season_number'] if 'season_number' in row.keys() else None
            if (is_manually_blacklisted(row['imdb_id'], season)
                    or is_manually_blacklisted(row['tmdb_id'], season)):
                skipped += 1
                continue

            conn.execute('''
                UPDATE media_items
                SET state = 'Wanted', last_updated = ?, sleep_cycles = 0,
                    wake_count = 0, next_retry_at = NULL, blacklisted_date = NULL,
                    last_scrape_failure = NULL
                WHERE id = ? AND state = 'Blacklisted'
                  AND (ghostlisted IS NULL OR ghostlisted = 0)
            ''', (datetime.now(), item_id))
            restored += 1
        conn.commit()
        logging.info(
            f"Removed {restored} items from blacklist "
            f"({skipped} skipped: ghostlisted, manually blacklisted, or not blacklisted)"
        )
    except Exception as e:
        logging.error(f"Error removing items from blacklist: {str(e)}")
        conn.rollback()
    finally:
        conn.close()