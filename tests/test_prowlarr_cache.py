"""Tests for the Prowlarr NZB query cache.

The cache reached 6 GB of resident memory in 17 hours on 2026-09-06. Its TTL
was only ever checked inside _cache_get, so an entry expired only if the exact
same query was asked again after the TTL had passed. A scrape queue working
through a backlog asks a different question every time, so almost every entry
was written once, never read, and never removed -- each one pinning a
~500-element result list whose dicts carry Prowlarr's recursive category tree.
"""
import importlib.util
import os
import sys
import types
import unittest


def _load_prowlarr_cache():
    """Load prowlarr.py's cache functions without importing the scraper package.

    The package __init__ pulls in settings, the database layer and PTT; the
    cache is none of those. Stub the two module-level imports it needs so the
    file can be exec'd on its own.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, 'scraper', 'prowlarr.py')
    src = open(path).read()
    # Take only the cache section: definitions through _cache_set.
    start = src.index('_NZB_CACHE: Dict[str, tuple]')
    end = src.index('def _build_prowlarr_params_list')
    body = (
        "import hashlib, threading, time\n"
        "from typing import Dict, List, Optional\n"
        + src[start:end]
    )
    mod = types.ModuleType('prowlarr_cache')
    exec(compile(body, path, 'exec'), mod.__dict__)
    return mod


_pc = _load_prowlarr_cache()


class TestCacheIsBounded(unittest.TestCase):
    def setUp(self):
        _pc._NZB_CACHE.clear()

    def test_unique_queries_do_not_accumulate_without_limit(self):
        """The regression: every key distinct, so nothing is ever re-read.

        Deliberately asserts an absolute ceiling rather than _NZB_CACHE_MAX, so
        it is a real failure against the unbounded version rather than an
        AttributeError on a constant that version does not have.
        """
        for i in range(8000):
            _pc._cache_set(f'key-{i}', [{'title': f'result {i}'}] * 500)
        self.assertLess(
            len(_pc._NZB_CACHE), 2000,
            f"cache holds {len(_pc._NZB_CACHE)} entries after 8000 unique "
            f"queries: nothing evicts on write, so the TTL never fires")

    def test_expired_entries_are_swept_on_write(self):
        now = _pc.time.monotonic()
        for i in range(_pc._NZB_CACHE_MAX):
            # Backdate past the TTL without waiting for it.
            _pc._NZB_CACHE[f'stale-{i}'] = (now - _pc._NZB_CACHE_TTL - 1, [1, 2, 3])
        _pc._cache_set('fresh', [4, 5, 6])
        self.assertNotIn('stale-0', _pc._NZB_CACHE)
        self.assertIn('fresh', _pc._NZB_CACHE)

    def test_live_entries_age_out_oldest_first_when_full(self):
        for i in range(_pc._NZB_CACHE_MAX):
            _pc._cache_set(f'live-{i}', [i])
        _pc._cache_set('newest', ['x'])
        self.assertIn('newest', _pc._NZB_CACHE)
        self.assertNotIn('live-0', _pc._NZB_CACHE)
        self.assertLessEqual(len(_pc._NZB_CACHE), _pc._NZB_CACHE_MAX)


class TestCacheStillCaches(unittest.TestCase):
    """Bounding it must not stop it doing its job."""

    def setUp(self):
        _pc._NZB_CACHE.clear()

    def test_a_repeated_query_within_ttl_is_served_from_cache(self):
        _pc._cache_set('k', [{'title': 'hit'}])
        self.assertEqual(_pc._cache_get('k'), [{'title': 'hit'}])

    def test_an_expired_entry_is_a_miss_and_is_dropped(self):
        _pc._NZB_CACHE['k'] = (_pc.time.monotonic() - _pc._NZB_CACHE_TTL - 1, ['old'])
        self.assertIsNone(_pc._cache_get('k'))
        self.assertNotIn('k', _pc._NZB_CACHE)

    def test_an_absent_key_is_a_miss(self):
        self.assertIsNone(_pc._cache_get('never-written'))


class WhatIsCached(unittest.TestCase):
    """Empties are the queries asked again most; an outage is a raise."""

    def setUp(self):
        _pc._NZB_CACHE.clear()

    def test_an_empty_list_is_a_hit(self):
        _pc._cache_set('k', [])
        self.assertEqual(_pc._cache_get('k'), [])

    def test_an_outage_round_trips_with_its_reason(self):
        _pc._cache_set('k', _pc._Unavailable('prowlarr down'), ttl=_pc._NZB_CACHE_UNAVAILABLE_TTL)
        cached = _pc._cache_get('k')
        self.assertIsInstance(cached, _pc._Unavailable)
        self.assertEqual(cached.reason, 'prowlarr down')

    def test_an_outage_expires_while_an_empty_of_the_same_age_is_live(self):
        _pc._cache_set('outage', _pc._Unavailable('x'), ttl=_pc._NZB_CACHE_UNAVAILABLE_TTL)
        _pc._cache_set('empty', [])
        real = _pc.time.monotonic
        _pc.time.monotonic = lambda: real() + _pc._NZB_CACHE_UNAVAILABLE_TTL + 1
        try:
            self.assertIsNone(_pc._cache_get('outage'))
            self.assertEqual(_pc._cache_get('empty'), [])
        finally:
            _pc.time.monotonic = real

    def test_the_cap_stays_well_under_the_leak_ceiling(self):
        self.assertLess(_pc._NZB_CACHE_MAX, 2000)


if __name__ == '__main__':
    unittest.main()
