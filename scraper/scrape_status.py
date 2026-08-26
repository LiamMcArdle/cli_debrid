"""Per-thread record of scrapers that could not be reached during a scrape.

A scraper that returns nothing because upstream genuinely has nothing and a
scraper that returns nothing because it was rate-limited or unreachable are
indistinguishable once both have produced an empty list. That distinction is
load bearing: the escalating retry ladder must not consume an item's retry
budget for a failure that never actually asked the question.

``ScraperManager`` records unreachable instances here while it fans out; the
Scraping queue reads them when a scrape produced no usable results. State is
thread-local so concurrent scrapes cannot see each other's failures, and it is
reset explicitly at the start of each top-level scrape.
"""

import threading

_local = threading.local()


class ScraperUnavailable(Exception):
    """A scrape did not complete. NOT the same as 'upstream returned nothing'."""


def reset_unavailable() -> None:
    """Clear the unavailable set for the current thread."""
    _local.unavailable = set()


def record_unavailable(instance: str) -> None:
    """Record that a scraper instance could not be reached."""
    if not hasattr(_local, 'unavailable'):
        _local.unavailable = set()
    _local.unavailable.add(instance)


def get_unavailable() -> set:
    """Return a copy of the scrapers that could not be reached on this thread."""
    return set(getattr(_local, 'unavailable', ()) or ())
