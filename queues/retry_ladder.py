"""Escalating retry ladder shared by the Scraping, Adding, Sleeping and Dormant queues.

A failed scrape or a failed add never blacklists an item. Instead the item walks
an escalating ladder of wake deadlines (30m -> 6h -> 1d -> 3d -> 7d) that is
persisted in the database -- ``media_items.sleep_cycles`` holds the rung index
and ``media_items.next_retry_at`` holds the absolute deadline -- so the backoff
survives process restarts and settings saves, neither of which the old in-memory
``SleepingQueue.sleeping_queue_times`` dict did.

When the ladder is exhausted the item moves to the terminal ``Dormant`` state,
which is re-checked every ``Queue.dormant_recheck_days``, forever. Nothing in
this module or its callers ever writes ``state='Blacklisted'``.

Timestamps are always Python ``datetime`` objects, never ISO strings. The
``last_updated``/``next_retry_at`` columns are written through the sqlite3
datetime adapter, which stores ``'YYYY-MM-DD HH:MM:SS.ffffff'`` with a SPACE
separator. Comparing that TEXT column against an ``isoformat()`` string would
silently mis-order rows, because ``'T'`` sorts above ``' '`` -- any deadline
later on the same calendar day would compare as already due.
"""

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from utilities.settings import get_setting

# 30 minutes -> 6 hours -> 1 day -> 3 days -> 7 days
DEFAULT_LADDER_MINUTES = [30, 360, 1440, 4320, 10080]

# Rung an item that is already well past its release date starts on. Back
# catalogue content does not become available again in 30 minutes, and the two
# short rungs would otherwise put tens of thousands of hopeless items through
# five scrape passes in eleven days. Index 2 == the 1 day rung.
DEFAULT_OLD_ITEM_START_RUNG = 2

# Cap on the serialized failure record so a pathological scrape cannot write a
# megabyte into the row.
MAX_FAILURE_RECORD_CHARS = 2000

# Timestamp formats accepted when reading a deadline back out of the DB.
_DEADLINE_FORMATS = (
    '%Y-%m-%d %H:%M:%S.%f',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%dT%H:%M:%S.%f',
    '%Y-%m-%dT%H:%M:%S',
)


def _parse_ladder(raw) -> List[int]:
    """Parse a ladder from a comma-separated string or a list of minutes."""
    if raw is None:
        return list(DEFAULT_LADDER_MINUTES)
    if isinstance(raw, str):
        raw = [part for part in raw.replace(' ', '').split(',') if part]
    if not isinstance(raw, (list, tuple)):
        logging.warning(f"Invalid retry ladder value {raw!r}. Using default ladder.")
        return list(DEFAULT_LADDER_MINUTES)
    ladder = []
    for part in raw:
        try:
            minutes = int(part)
        except (TypeError, ValueError):
            logging.warning(f"Invalid retry ladder entry {part!r}. Ignoring it.")
            continue
        if minutes > 0:
            ladder.append(minutes)
    return ladder


def get_ladder(version: Optional[str] = None) -> List[int]:
    """Return the retry ladder, in minutes, for a version.

    This is the ONLY reader of the retry setting anywhere in the codebase. The
    per-version ``retry_ladder_minutes`` wins over the global
    ``Queue.retry_ladder_minutes``; an empty per-version value inherits the
    global. An empty ladder means 'no retries' -- the first failure goes
    straight to Dormant, which replaces the old ``wake_count: -1`` behaviour.

    Replaces the two divergent keys this used to be split across:
    ``max_wake_count`` (read only by ScrapingQueue, present in no schema and no
    config, so it always fell through to the global) and ``wake_count`` (read
    only by SleepingQueue, and colliding by name with the media_items.wake_count
    column).
    """
    if version:
        try:
            version_settings = (get_setting('Scraping', 'versions', {}) or {}).get(version, {}) or {}
        except Exception:
            version_settings = {}
        override = version_settings.get('retry_ladder_minutes')
        if override not in (None, ''):
            return _parse_ladder(override)
    return _parse_ladder(
        get_setting('Queue', 'retry_ladder_minutes',
                    ','.join(str(m) for m in DEFAULT_LADDER_MINUTES))
    )


def get_dormant_interval() -> timedelta:
    """How long a Dormant item waits between re-checks."""
    try:
        days = float(get_setting('Queue', 'dormant_recheck_days', 7))
    except (TypeError, ValueError):
        days = 7.0
    if days <= 0:
        days = 7.0
    return timedelta(days=days)


def next_rung(item: Dict[str, Any], is_old: bool = False) -> int:
    """Return the rung the item should move to after a failure.

    ``sleep_cycles`` holds the rung the item is currently ON; 0 means it has not
    failed yet. The returned value is written back as the new ``sleep_cycles``.
    A return value greater than ``len(ladder)`` means the ladder is exhausted.

    ``is_old`` only shifts the STARTING rung, and only for an item that has not
    failed yet. It never shortens the ladder for an item already climbing it,
    and it never routes anywhere except further up the ladder.
    """
    try:
        current = int(item.get('sleep_cycles') or 0)
    except (TypeError, ValueError):
        current = 0
    if current < 0:
        current = 0
    if current == 0 and is_old:
        try:
            start = int(get_setting('Queue', 'retry_ladder_old_item_start_rung',
                                    DEFAULT_OLD_ITEM_START_RUNG))
        except (TypeError, ValueError):
            start = DEFAULT_OLD_ITEM_START_RUNG
        current = max(0, start)
    return current + 1


def deadline_for_rung(rung: int, ladder: List[int],
                      now: Optional[datetime] = None) -> Optional[datetime]:
    """Absolute wake time for a rung, or None when the ladder is exhausted."""
    if now is None:
        now = datetime.now()
    if rung < 1 or rung > len(ladder):
        return None
    return now + timedelta(minutes=ladder[rung - 1])


def parse_deadline(value) -> Optional[datetime]:
    """Parse a next_retry_at value out of a DB row. None means 'due now'."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    for fmt in _DEADLINE_FORMATS:
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    logging.warning(f"Unparseable next_retry_at value {value!r}; treating item as due.")
    return None


def build_failure_record(stage: str, raw_count: int = 0, passed_count: int = 0,
                         filtered_out: Optional[List[Dict[str, Any]]] = None,
                         error: Optional[str] = None,
                         rung: Optional[int] = None,
                         next_retry_at: Optional[datetime] = None,
                         unavailable_scrapers: Optional[List[str]] = None) -> str:
    """Serialize a compact, durable record of why an attempt produced nothing.

    Nothing in the schema recorded this before: filter_results.py sets
    ``filter_reason`` on every rejected result and then drops it on the floor,
    and ``scrape_results`` only ever holds ACCEPTED candidates -- and is NULLed
    on every terminal transition anyway. Written to
    ``media_items.last_scrape_failure``.

    The single most valuable field is ``raw``: it is what separates 'upstream
    returned nothing' (a dead season/episode coordinate) from 'we got N results
    and rejected all N' (a filter problem). ``unavail`` separates both of those
    from 'the scraper never answered' (a rate limit or an outage).
    """
    record = {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'stage': stage,
        'raw': int(raw_count or 0),
        'passed': int(passed_count or 0),
    }
    if rung is not None:
        record['rung'] = rung
    if next_retry_at is not None:
        record['next'] = next_retry_at.isoformat(timespec='seconds')
    if error:
        record['error'] = str(error)[:200]
    if unavailable_scrapers:
        record['unavail'] = sorted(str(s) for s in unavailable_scrapers)[:8]

    if filtered_out:
        reasons = Counter(
            (r.get('filter_reason') or 'Unknown')
            for r in filtered_out if isinstance(r, dict)
        )
        record['reasons'] = dict(reasons.most_common(8))
        examples = []
        seen = set()
        for r in filtered_out:
            if not isinstance(r, dict):
                continue
            reason = r.get('filter_reason') or 'Unknown'
            if reason in seen:
                continue
            seen.add(reason)
            title = r.get('original_title') or r.get('title') or ''
            examples.append([reason, str(title)[:120]])
            if len(examples) >= 5:
                break
        if examples:
            record['examples'] = examples

    try:
        blob = json.dumps(record, separators=(',', ':'))
    except (TypeError, ValueError):
        blob = json.dumps({'ts': record['ts'], 'stage': stage,
                           'raw': record['raw'], 'passed': record['passed']})
    if len(blob) > MAX_FAILURE_RECORD_CHARS:
        record.pop('examples', None)
        blob = json.dumps(record, separators=(',', ':'))[:MAX_FAILURE_RECORD_CHARS]
    return blob
