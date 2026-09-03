import logging
from datetime import datetime
from typing import Any, Dict

from utilities.settings import get_setting


class DormantQueue:
    """Long-interval home for items the retry ladder could not fill.

    An item lands here after walking the whole escalating ladder without a
    single usable scrape result. It keeps its row, its version and its
    ``last_scrape_failure`` record, and it is re-scraped every
    ``Queue.dormant_recheck_days``. After ``Queue.dormant_cycles_before_blacklist``
    such cycles QueueManager.move_to_dormant blacklists that one item instead
    (stage ``'exhausted'``); nothing in this queue writes that state itself.

    Like BlacklistedQueue and UnreleasedQueue this is DB-backed: the population
    is expected to be tens of thousands of rows, so it is never loaded into
    memory.
    """

    def __init__(self):
        logging.info("DormantQueue initialized (DB-backed, no in-memory items).")

    def update(self):
        pass

    def get_contents(self):
        return []

    def add_item(self, item: Dict[str, Any]):
        logging.debug(f"DormantQueue.add_item called for ID {item.get('id', 'N/A')} - item state managed in DB.")

    def remove_item(self, item: Dict[str, Any]):
        logging.debug(f"DormantQueue.remove_item called for ID {item.get('id', 'N/A')} - item state managed in DB.")

    def contains_item_id(self, item_id):
        from database.core import get_db_connection
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.execute(
                "SELECT 1 FROM media_items WHERE id = ? AND state = 'Dormant' LIMIT 1",
                (item_id,)
            )
            return cursor.fetchone() is not None
        except Exception as e:
            logging.error(f"Error checking DB for dormant item ID {item_id}: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def process(self, queue_manager):
        """Wake a bounded batch of Dormant items whose re-check deadline passed.

        The per-item schedule lives in media_items.next_retry_at, so this task's
        interval is only a polling rate -- it is safe for the scheduler to jitter
        or delay it, and a restart cannot lose an item's place.

        The deadline parameter is a datetime OBJECT, not an ISO string: the
        column is written through the sqlite3 datetime adapter as
        'YYYY-MM-DD HH:MM:SS.ffffff' with a space separator, and comparing that
        against an isoformat string would treat every deadline later on the same
        calendar day as already due, collapsing the whole backoff.
        """
        try:
            batch_size = int(get_setting("Queue", "dormant_batch_size", 150))
        except (TypeError, ValueError):
            batch_size = 150
        if batch_size <= 0:
            logging.debug("Dormant batch size is zero or negative, skipping re-check sweep.")
            return

        from database.core import get_db_connection

        now = datetime.now()
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.execute("""
                SELECT * FROM media_items
                WHERE state = 'Dormant'
                  AND (ghostlisted IS NULL OR ghostlisted = 0)
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY next_retry_at ASC
                LIMIT ?
            """, (now, batch_size))
            due_items = [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logging.error(f"Error querying Dormant items due for re-check: {e}", exc_info=True)
            return
        finally:
            if conn:
                conn.close()

        if not due_items:
            return

        if len(due_items) >= batch_size:
            logging.warning(
                f"Dormant sweep returned a full batch of {batch_size} items — the sweep may be "
                f"falling behind its configured re-check interval. Consider raising "
                f"Queue.dormant_batch_size or Queue.dormant_recheck_days."
            )

        logging.info(f"Waking {len(due_items)} Dormant item(s) for their periodic re-check.")
        for item in due_items:
            try:
                item_identifier = queue_manager.generate_identifier(item)
                # sleep_cycles is deliberately NOT reset here. The item keeps its
                # terminal rung, so if this re-check also fails it returns
                # straight to Dormant with a fresh deadline rather than replaying
                # the whole 30m -> 7d ladder every cycle.
                queue_manager.move_to_wanted(item, "Dormant")
                logging.info(f"Dormant re-check: moved {item_identifier} back to Wanted.")
            except Exception as e:
                logging.error(f"Error waking Dormant item ID {item.get('id')}: {e}", exc_info=True)
