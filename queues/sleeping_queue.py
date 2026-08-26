import logging
from typing import Dict, Any
from datetime import datetime

from utilities.settings import get_setting
from database.core import get_db_connection
from queues.retry_ladder import parse_deadline


class SleepingQueue:
    """Holds items waiting out a rung of the escalating retry ladder.

    This queue holds NO timing state of its own. The wake deadline is
    ``media_items.next_retry_at`` and the ladder position is
    ``media_items.sleep_cycles``, both written by
    ``QueueManager.advance_retry_ladder``. That is what makes a 7-day rung
    survive a container restart or a settings save -- the old in-memory
    ``sleeping_queue_times`` dict survived neither, and it was re-stamped with
    ``datetime.now()`` on every ``update()`` and on every
    ``QueueManager.reinitialize()``.

    It also no longer decides anything: it does not count wakes, it does not
    read ``wake_limit``, and it cannot blacklist. Ladder exhaustion is decided
    once, at ladder-advance time, and produces Dormant.

    ``process()`` selects due rows straight from the database rather than
    filtering ``self.items``. ``self.items`` is only a display cache populated
    by the separate 30s queue-view task, so relying on it would (a) strand every
    Sleeping row if that task were disabled and (b) wake items whose state had
    changed underneath the snapshot, yanking an in-flight download back to
    Wanted and losing its torrent association.
    """

    def __init__(self):
        self.items = []

    def update(self):
        from database import get_all_media_items
        # get_all_media_items does SELECT *, so wake_count / sleep_cycles /
        # next_retry_at are already on the row. The old per-item get_wake_count()
        # loop opened and closed a DB connection for every row, which was free
        # when one item was asleep and is not once Sleeping is the dominant state.
        self.items = [dict(row) for row in get_all_media_items(state="Sleeping")]
        for item in self.items:
            if item.get('wake_count') is None:
                item['wake_count'] = 0

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        # Idempotent, mirroring FinalCheckQueue.add_item. The old unconditional
        # append produced a duplicate entry and a second 'sleeping' notification
        # on every sleep, because QueueManager.move_to_sleeping added the item a
        # second time after _move_item_to_queue had already added it.
        item_id = item.get('id')
        if item_id is not None and any(i.get('id') == item_id for i in self.items):
            return

        if item.get('wake_count') is None:
            from database import get_wake_count
            item['wake_count'] = get_wake_count(item['id'])
        self.items.append(item)
        logging.debug(
            f"Added item to Sleeping queue: {item_id} "
            f"(ladder rung {item.get('sleep_cycles')}, wake at {item.get('next_retry_at')})"
        )

        from routes.notifications import send_notifications
        from routes.settings_routes import get_enabled_notifications, get_enabled_notifications_for_category
        from routes.extensions import app

        # Send notification for the state change
        try:
            with app.app_context():
                response = get_enabled_notifications_for_category('sleeping')
                if response.json['success']:
                    enabled_notifications = response.json['enabled_notifications']
                    if enabled_notifications:
                        notification_data = {
                            'id': item['id'],
                            'title': item.get('title', 'Unknown Title'),
                            'type': item.get('type', 'unknown'),
                            'year': item.get('year', ''),
                            'version': item.get('version', ''),
                            'season_number': item.get('season_number'),
                            'episode_number': item.get('episode_number'),
                            'new_state': 'Sleeping',
                            'is_upgrade': False,
                            'upgrading_from': None
                        }
                        send_notifications([notification_data], enabled_notifications, notification_category='state_change')
        except Exception as e:
            logging.error(f"Failed to send state change notification: {str(e)}")

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]
        logging.debug(f"Removed item from Sleeping queue: {item['id']}")

    def process(self, queue_manager):
        """Wake every item whose persisted retry deadline has passed.

        The deadline parameter is a datetime OBJECT, not an ISO string -- see
        the note in queues/retry_ladder.py about the space separator the sqlite3
        adapter writes.
        """
        try:
            batch_size = int(get_setting("Queue", "sleeping_batch_size", 250))
        except (TypeError, ValueError):
            batch_size = 250
        if batch_size <= 0:
            batch_size = 250

        now = datetime.now()
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.execute("""
                SELECT * FROM media_items
                WHERE state = 'Sleeping'
                  AND (ghostlisted IS NULL OR ghostlisted = 0)
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY next_retry_at ASC
                LIMIT ?
            """, (now, batch_size))
            items_to_wake = [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logging.error(f"Error querying Sleeping items due to wake: {e}", exc_info=True)
            return
        finally:
            if conn:
                conn.close()

        if items_to_wake:
            self.wake_items(queue_manager, items_to_wake)

    def wake_items(self, queue_manager, items):
        logging.debug(f"Attempting to wake {len(items)} items")
        from database import increment_wake_count
        for item in items:
            item_id = item['id']
            item_identifier = queue_manager.generate_identifier(item)
            old_wake_count = item.get('wake_count') or 0

            # Informational lifetime counter only -- nothing gates on it any more.
            new_wake_count = increment_wake_count(item_id)
            queue_manager.move_to_wanted(item, "Sleeping")
            logging.info(
                f"Moved item {item_identifier} from Sleeping to Wanted queue "
                f"(ladder rung {item.get('sleep_cycles')}, lifetime wakes: "
                f"{old_wake_count} -> {new_wake_count})"
            )

        logging.debug(f"Woke up {len(items)} items")

    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID"""
        return any(i['id'] == item_id for i in self.items)
