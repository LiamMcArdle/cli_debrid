"""Record of scrapers that could not be reached during a scrape.

A scraper that returns nothing because upstream genuinely has nothing and a
scraper that returns nothing because it was rate-limited or unreachable are
indistinguishable once both have produced an empty list. That distinction is
load bearing: the escalating retry ladder must not consume an item's retry
budget for a failure that never actually asked the question.

``ScraperManager`` records unreachable instances here while it fans out; the
Scraping queue reads them when a scrape produced no usable results.

The two are several threads apart. ``scrape`` runs on a queue worker, fans
titles out across one ``ThreadPoolExecutor``, and each of those workers fans
scrapers out across another -- so the write happens two pools below the read.
``threading.local`` does not follow a task into a pool, and those pools are
shut down as soon as the scrape ends, so anything recorded on a worker's own
thread-local is silently discarded.

So the thread-local holds a *live set object* rather than the records
themselves. ``reset_unavailable`` creates it and hands it back; the scrape
threads it down to the workers, which mutate that same object under a lock.
Concurrent scrapes still cannot see each other's failures, because each one
starts from its own set.
"""

import threading

_local = threading.local()
_lock = threading.Lock()


class ScraperUnavailable(Exception):
    """A scrape did not complete. NOT the same as 'upstream returned nothing'."""


def reset_unavailable() -> set:
    """Start a new scope for this thread and return the live set backing it.

    Pass the returned set down to any worker thread that might record a
    failure; see the module docstring for why it cannot find its own.
    """
    scope = set()
    _local.unavailable = scope
    return scope


def current_scope() -> set:
    """Return the live set for this thread, creating one if absent."""
    if not hasattr(_local, 'unavailable'):
        _local.unavailable = set()
    return _local.unavailable


def record_unavailable(instance: str, scope: set = None) -> None:
    """Record that a scraper instance could not be reached.

    ``scope`` is the set handed down by the thread that started the scrape.
    Workers must pass it -- without it the record lands on the worker's own
    thread-local and is lost when its pool is shut down.
    """
    target = scope if scope is not None else current_scope()
    with _lock:
        target.add(instance)


def get_unavailable() -> set:
    """Return a copy of the scrapers that could not be reached on this thread."""
    with _lock:
        return set(getattr(_local, 'unavailable', ()) or ())
