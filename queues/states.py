"""Item-state groupings that more than one queue has to agree on."""

import json
from typing import Any, Dict

# States whose episodes a season pack may fill on its way through Adding. A
# pack is free supply: when one lands for a show, every sibling that is still
# waiting should take its file, whether that sibling happens to be in Wanted or
# parked in Sleeping/Dormant by the retry ladder. Blacklisted is included ONLY
# for rows the ladder exhausted -- see is_exhausted_blacklist -- never for a
# row a person blacklisted.
RELATED_FILL_STATES = ('Wanted', 'Scraping', 'Sleeping', 'Dormant', 'Blacklisted')

EXHAUSTED_STAGE = 'exhausted'


def failure_stage(item: Dict[str, Any]):
    """The ``stage`` recorded in the item's last_scrape_failure, or None."""
    try:
        record = json.loads(item.get('last_scrape_failure') or '{}')
    except (TypeError, ValueError):
        return None
    return record.get('stage') if isinstance(record, dict) else None


def is_exhausted_blacklist(item: Dict[str, Any]) -> bool:
    """A row the retry ladder blacklisted, that a person has not also blacklisted.

    Such a row is still wanted -- nothing could be found for it -- so a pack
    that turns up may fill it. A manual blacklist or ghostlist means the
    opposite and is never filled.
    """
    if item.get('state') != 'Blacklisted':
        return False
    if failure_stage(item) != EXHAUSTED_STAGE:
        return False
    from database.blacklist import is_restorable_blacklist_item
    return is_restorable_blacklist_item(item)


def is_fillable_by_pack(item: Dict[str, Any]) -> bool:
    """Whether a related-item pack fill may take this row."""
    state = item.get('state')
    if state == 'Blacklisted':
        return is_exhausted_blacklist(item)
    return state in RELATED_FILL_STATES
