"""A provider refusing a hash is neither an outage nor a broken torrent.

Real-Debrid answered HTTP 451 to 20,661 addMagnet calls over 12 days
(2026-08-29 -> 09-08), per hash: AnimeTosho fansubs 0/282, Zilean hashes
44/44, in the same minutes. Every one of those hashes went into the global
not_wanted store, every affected item spent a retry rung, and one debrid
add failure switched a whole season tail to single-episode scraping. A real
outage (5xx, timeouts) went the same way. These tests pin the split:

  451             -> recorded against that provider only; next candidate
  5xx / timeout   -> provider parked; nothing recorded; item waits
  all refused     -> held as 'provider_blocked'; no rung, no sibling sweep
"""

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import database  # noqa: F401  (circular-import guard, must come first)

import requests

from debrid.base import ContentBlockedError, ProviderUnavailableError
from debrid.real_debrid import api as rd_api
from debrid.real_debrid.exceptions import RealDebridAPIError
import database.provider_blocks as pb
from scraper import park

MAGNET = 'magnet:?xt=urn:btih:' + 'a' * 40
HASH = 'a' * 40


def _resp(status, body=None, text=''):
    r = MagicMock()
    r.status_code = status
    r.headers = {}
    r.text = text or (json.dumps(body) if body is not None else '')
    if body is None:
        r.json.side_effect = ValueError('not json')
    else:
        r.json.return_value = body
    return r


def _raised(resp):
    """What api_tracker actually does: raise_for_status() before returning.

    The first deploy classified 451 only on a returned response and so never
    fired live -- six real 451s parked the provider as an outage instead."""
    return requests.exceptions.HTTPError(f"{resp.status_code} Client Error", response=resp)


class TestApiLayerClassifies451(unittest.TestCase):
    def test_451_raised_by_api_tracker_becomes_content_blocked(self):
        resp = _resp(451, {'error': 'infringing_file', 'error_code': 35})
        with patch.object(rd_api.api, 'post', side_effect=_raised(resp)), \
                patch.object(rd_api, '_wait_for_rate_limit'):
            with self.assertRaises(ContentBlockedError) as cm:
                rd_api.make_request('POST', '/torrents/addMagnet', 'key', data={'magnet': MAGNET})
        self.assertEqual(cm.exception.error, 'infringing_file')
        self.assertEqual(cm.exception.error_code, 35)

    def test_451_returned_as_a_response_also_classifies(self):
        with patch.object(rd_api.api, 'post', return_value=_resp(451, {'error': 'infringing_file', 'error_code': 35})), \
                patch.object(rd_api, '_wait_for_rate_limit'):
            with self.assertRaises(ContentBlockedError):
                rd_api.make_request('POST', '/torrents/addMagnet', 'key', data={'magnet': MAGNET})

    def test_451_without_json_body_still_classifies(self):
        resp = _resp(451, None, text='Unavailable')
        with patch.object(rd_api.api, 'post', side_effect=_raised(resp)), \
                patch.object(rd_api, '_wait_for_rate_limit'):
            with self.assertRaises(ContentBlockedError) as cm:
                rd_api.make_request('POST', '/torrents/addMagnet', 'key', data={})
        self.assertIsNone(cm.exception.error_code)

    def test_a_raised_503_is_still_provider_level(self):
        for fn in (rd_api.make_request, rd_api.make_request_strict):
            fn.retry.sleep = lambda *_: None
        with patch.object(rd_api.api, 'post', side_effect=_raised(_resp(503, None, text='down'))), \
                patch.object(rd_api, '_wait_for_rate_limit'):
            with self.assertRaises(ProviderUnavailableError):
                rd_api.make_request_strict('POST', '/torrents/addMagnet', 'key', data={})

    def test_451_on_user_still_returns_the_user(self):
        with patch.object(rd_api.api, 'get', return_value=_resp(451, {'id': 1, 'username': 'x'})), \
                patch.object(rd_api, '_wait_for_rate_limit'):
            self.assertEqual(rd_api.make_request('GET', '/user', 'key')['id'], 1)

    def test_strict_form_raises_where_legacy_form_returns_none(self):
        """An exhausted retry used to come back as None, which add_torrent read as
        RD's duplicate-magnet 404 and is_cached turned into 'not cached'."""
        for fn in (rd_api.make_request, rd_api.make_request_strict):
            fn.retry.sleep = lambda *_: None
        with patch.object(rd_api.api, 'post', side_effect=requests.exceptions.ConnectionError('down')), \
                patch.object(rd_api, '_wait_for_rate_limit'):
            self.assertIsNone(rd_api.make_request('POST', '/torrents/addMagnet', 'key', data={}))
            with self.assertRaises(RealDebridAPIError):
                rd_api.make_request_strict('POST', '/torrents/addMagnet', 'key', data={})


class _TempStore(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        path = os.path.join(self._dir, 'media_items.db')
        self._patch = patch.object(pb, 'get_db_connection', side_effect=lambda: sqlite3.connect(path))
        self._patch.start()
        pb._CACHE.clear(); pb._CACHE_LOADED_AT = 0.0

    def tearDown(self):
        self._patch.stop()
        pb._CACHE.clear(); pb._CACHE_LOADED_AT = 0.0


class TestProviderBlockStore(_TempStore):
    def test_a_block_is_per_provider(self):
        self.assertTrue(pb.record_block(HASH, 'Real-Debrid', reason='infringing_file', error_code=35))
        self.assertFalse(pb.record_block(HASH, 'Real-Debrid'), 'second record is not new')
        self.assertTrue(pb.is_blocked(HASH, 'Real-Debrid'))
        self.assertFalse(pb.is_blocked(HASH, 'TorBox'))
        self.assertTrue(pb.is_blocked_everywhere(HASH, ['Real-Debrid']))
        self.assertFalse(pb.is_blocked_everywhere(HASH, ['Real-Debrid', 'TorBox']),
                         'a fallback provider that has not refused it may still serve it')
        self.assertFalse(pb.is_blocked_everywhere(HASH, []))

    def test_reason_and_code_are_kept(self):
        pb.record_block(HASH, 'Real-Debrid', reason='infringing_file', error_code=35)
        conn = pb.get_db_connection()
        row = conn.execute('SELECT reason, error_code, blocked_at FROM provider_blocks').fetchone()
        conn.close()
        self.assertEqual(row[0], 'infringing_file'); self.assertEqual(row[1], 35); self.assertTrue(row[2])

    def test_survives_a_cache_reload(self):
        pb.record_block(HASH, 'Real-Debrid')
        pb._CACHE.clear(); pb._CACHE_LOADED_AT = 0.0
        self.assertTrue(pb.is_blocked(HASH, 'Real-Debrid'))

    def test_scrape_time_filter_uses_configured_providers(self):
        pb.record_block(HASH, 'Real-Debrid')
        with patch.object(pb, 'configured_provider_names', return_value=['Real-Debrid']):
            self.assertTrue(pb.is_result_blocked_everywhere(MAGNET))
        with patch.object(pb, 'configured_provider_names', return_value=['Real-Debrid', 'TorBox']):
            self.assertFalse(pb.is_result_blocked_everywhere(MAGNET))
        self.assertFalse(pb.is_result_blocked_everywhere(None))
        self.assertFalse(pb.is_result_blocked_everywhere('https://example/file.torrent'))


def _rd_client():
    from debrid.real_debrid.client import RealDebridProvider
    c = RealDebridProvider.__new__(RealDebridProvider)
    c.phalanx_enabled = False
    c.phalanx_cache = None
    c._all_torrent_ids = {}
    c._cached_torrent_ids = {}
    c._cached_torrent_titles = {}
    c.update_status = MagicMock()
    c.remove_torrent = MagicMock()
    c.get_torrent_info = MagicMock(return_value=None)
    return c


class TestCacheCheckSplitsRefusalFromOutage(unittest.TestCase):
    def _run(self, client):
        return asyncio.run(client.is_cached(MAGNET, result_title='t'))

    def test_451_records_against_the_provider_and_never_not_wanted(self):
        c = _rd_client()
        c.add_torrent = MagicMock(side_effect=ContentBlockedError('refused', error='infringing_file', error_code=35))
        with patch('debrid.real_debrid.client.record_block') as rec, \
                patch('debrid.real_debrid.client.add_to_not_wanted') as nw, \
                patch('debrid.real_debrid.client.trip_debrid') as trip:
            self.assertIsNone(self._run(c))
        rec.assert_called_once_with(HASH, 'Real-Debrid', reason='infringing_file', error_code=35)
        nw.assert_not_called()
        trip.assert_not_called()
        c.remove_torrent.assert_not_called()

    def test_outage_parks_the_provider_and_records_nothing(self):
        c = _rd_client()
        c.add_torrent = MagicMock(side_effect=ProviderUnavailableError('Request failed: 503'))
        with patch('debrid.real_debrid.client.record_block') as rec, \
                patch('debrid.real_debrid.client.add_to_not_wanted') as nw, \
                patch('debrid.real_debrid.client.trip_debrid') as trip:
            self.assertIsNone(self._run(c))
        trip.assert_called_once()
        rec.assert_not_called()
        nw.assert_not_called()

    def test_info_fetch_failure_after_add_is_an_outage_not_a_bad_hash(self):
        c = _rd_client()
        c.add_torrent = MagicMock(return_value='TID')
        with patch('debrid.real_debrid.client.add_to_not_wanted') as nw, \
                patch('debrid.real_debrid.client.trip_debrid') as trip:
            self.assertIsNone(self._run(c))
        trip.assert_called_once()
        nw.assert_not_called()
        c.remove_torrent.assert_called_once()


def _processor(provider):
    from queues.torrent_processor import TorrentProcessor
    tp = TorrentProcessor.__new__(TorrentProcessor)
    tp.debrid_provider = provider
    tp.process_torrent = MagicMock(side_effect=lambda link: (link, None))
    return tp


class TestOneCacheCheckPerHashPerItem(unittest.TestCase):
    def setUp(self):
        self.provider = MagicMock(PROVIDER_NAME='Real-Debrid')
        self.tp = _processor(self.provider)
        self.results = [{'title': 'A', 'magnet': MAGNET}, {'title': 'A again', 'magnet': MAGNET}]
        self.item = {'id': 1, 'title': 'Show'}

    def test_duplicate_sources_and_the_hybrid_second_pass_share_one_check(self):
        with patch('queues.torrent_processor.get_debrid_providers', return_value=[self.provider]), \
                patch('queues.torrent_processor.is_blocked_everywhere', return_value=False), \
                patch.object(self.tp, 'check_cache_status', return_value=(False, 'direct_check')) as chk:
            self.tp.process_results(self.results, accept_uncached=False, item=self.item)
            self.tp.process_results(self.results, accept_uncached=False, item=self.item)
        self.assertEqual(chk.call_count, 1, 'two results x two passes, one round trip')
        self.assertEqual(self.item['_provider_blocked'], {'blocked': 0, 'total': 2, 'providers': ['Real-Debrid']})
        json.dumps(self.item['_cache_memo'])  # primitives only; the item may be serialised

    def test_every_candidate_refused_is_counted_without_a_round_trip(self):
        with patch('queues.torrent_processor.get_debrid_providers', return_value=[self.provider]), \
                patch('queues.torrent_processor.is_blocked_everywhere', return_value=True), \
                patch.object(self.tp, 'check_cache_status') as chk:
            out = self.tp.process_results(self.results, accept_uncached=True, item=self.item)
        self.assertEqual(out, (None, None, None))
        chk.assert_not_called()
        self.assertEqual(self.item['_provider_blocked']['blocked'], 2)
        self.assertEqual(self.item['_provider_blocked']['total'], 2)

    def test_a_refusal_discovered_during_the_check_counts_too(self):
        blocked = {'v': False}
        def check(*a, **k):
            blocked['v'] = True
            return (None, 'error')
        with patch('queues.torrent_processor.get_debrid_providers', return_value=[self.provider]), \
                patch('queues.torrent_processor.is_blocked_everywhere', side_effect=lambda *a: blocked['v']), \
                patch.object(self.tp, 'check_cache_status', side_effect=check):
            self.tp.process_results(self.results[:1], accept_uncached=True, item=self.item)
        self.assertEqual(self.item['_provider_blocked'], {'blocked': 1, 'total': 1, 'providers': ['Real-Debrid']})


class TestAllRefusedIsHeldNotFailed(unittest.TestCase):
    def test_routes_to_a_provider_blocked_hold_without_the_sibling_sweep(self):
        from queues.adding_queue import AddingQueue, PROVIDER_BLOCKED_PREFIX
        aq = AddingQueue.__new__(AddingQueue)
        aq.items = []
        qm = MagicMock()
        item = {'id': 7, 'type': 'episode', 'title': 'Show', 'season_number': 1, 'episode_number': 2}
        with patch('database.get_media_item_by_id', create=True) as get_item, \
                patch('database.update_media_item', create=True) as update:
            aq._handle_failed_item(item, f"{PROVIDER_BLOCKED_PREFIX}2 candidate(s) refused by Real-Debrid", qm)
        qm.advance_retry_ladder.assert_called_once()
        args, kwargs = qm.advance_retry_ladder.call_args
        self.assertTrue(kwargs['hold_rung'])
        self.assertEqual(json.loads(kwargs['failure_record'])['stage'], 'provider_blocked')
        qm.move_to_scraping.assert_not_called()
        get_item.assert_not_called()
        update.assert_not_called()

    def test_any_other_add_failure_still_takes_the_old_path(self):
        from queues.adding_queue import AddingQueue
        aq = AddingQueue.__new__(AddingQueue)
        aq.items = []
        qm = MagicMock()
        item = {'id': 7, 'type': 'movie', 'title': 'Film'}
        with patch('database.get_media_item_by_id', create=True, return_value={'fall_back_to_single_scraper': True}), \
                patch('database.update_media_item', create=True):
            aq._handle_failed_item(item, "No valid results found after cache/uncached processing", qm)
        args, kwargs = qm.advance_retry_ladder.call_args
        self.assertEqual(json.loads(kwargs['failure_record'])['stage'], 'add_failed')
        self.assertFalse(kwargs.get('hold_rung', False))


class TestNoSiblingSweep(unittest.TestCase):
    def test_a_failure_flags_only_the_item_itself(self):
        """One item finding nothing addable said nothing about its siblings,
        yet the flag was copied onto every later episode of the season: 50
        Pokemon rows in one stroke on 2026-09-09."""
        from queues.adding_queue import AddingQueue
        aq = AddingQueue.__new__(AddingQueue)
        aq.items = []
        qm = MagicMock()
        item = {'id': 7, 'type': 'episode', 'title': 'Pokemon', 'season_number': 18,
                'episode_number': 95, 'version': 'Anime'}
        with patch('database.get_media_item_by_id', create=True, return_value={'fall_back_to_single_scraper': False}), \
                patch('database.update_media_item', create=True) as update, \
                patch('database.stream_all_media_items', create=True) as stream, \
                patch('queues.adding_queue.get_setting', return_value=True):
            aq._handle_failed_item(item, "No valid results found after cache/uncached processing", qm)
        update.assert_called_once_with(7, fall_back_to_single_scraper=True)
        stream.assert_not_called()
        qm.move_to_scraping.assert_called_once()


def _manager():
    from queues.queue_manager import QueueManager
    qm = QueueManager.__new__(QueueManager)
    qm._move_item_to_queue = MagicMock(return_value=None)
    qm.move_to_blacklisted = MagicMock()
    qm.move_to_sleeping = MagicMock()
    qm.move_to_dormant = MagicMock()
    return qm


_ITEM = {'id': 1, 'type': 'episode', 'title': 'Show', 'imdb_id': 'tt1',
         'season_number': 1, 'episode_number': 2, 'version': 'Anime', 'sleep_cycles': 3}
_DEFAULTS = lambda section, key, d=None: d


class TestProviderBlockedHoldsTheRung(unittest.TestCase):
    def test_holds_at_the_current_rung_for_the_dormant_interval_uncapped(self):
        qm = _manager()
        record = json.dumps({'stage': 'provider_blocked', 'error': 'all candidates refused', 'holds': 999})
        with patch('queues.queue_manager.get_setting', side_effect=_DEFAULTS), \
                patch('queues.retry_ladder.get_setting', side_effect=_DEFAULTS):
            qm.advance_retry_ladder(dict(_ITEM, last_scrape_failure=record), "Adding",
                                    failure_record=record, hold_rung=True)
        qm.move_to_sleeping.assert_called_once()
        kwargs = qm.move_to_sleeping.call_args.kwargs
        self.assertEqual(kwargs['rung'], 3, 'rung is held, not advanced')
        delay = kwargs['next_retry_at'] - datetime.now()
        self.assertGreater(delay, timedelta(days=6.9))
        self.assertLess(delay, timedelta(days=8.5))
        qm.move_to_dormant.assert_not_called()

    def test_reaching_dormant_on_a_refusal_neither_counts_nor_blacklists(self):
        qm = _manager()
        del qm.move_to_dormant  # exercise the real terminal site
        with patch('queues.queue_manager.get_setting',
                   side_effect=lambda s, key, d=None: 3 if key == 'dormant_cycles_before_blacklist' else d), \
                patch('database.update_media_item', create=True):
            qm.move_to_dormant(dict(_ITEM, dormant_cycles=2), "Adding",
                               failure_record=json.dumps({'stage': 'provider_blocked'}))
        qm.move_to_blacklisted.assert_not_called()
        self.assertEqual(qm._move_item_to_queue.call_args.kwargs['dormant_cycles'], 2)

    def test_waking_from_a_refusal_hold_counts_as_a_hold(self):
        from queues.queue_manager import QueueManager
        self.assertTrue(QueueManager._woke_from_hold({'last_scrape_failure': json.dumps({'stage': 'provider_blocked'})}))
        self.assertTrue(QueueManager._woke_from_hold({'last_scrape_failure': json.dumps({'stage': 'scrape_unavailable'})}))
        self.assertFalse(QueueManager._woke_from_hold({'last_scrape_failure': json.dumps({'stage': 'add_failed'})}))


class TestDebridPark(unittest.TestCase):
    def test_parks_on_first_use_and_clears_on_success(self):
        park.DEBRID_PARKS.pop('X', None)
        self.assertEqual(park.debrid_park_remaining('X'), 0.0)
        self.assertGreater(park.trip_debrid('X', 'down'), 0)
        self.assertGreater(park.debrid_park_remaining('X'), 0)
        park.clear_debrid('X')
        self.assertEqual(park.debrid_park_remaining('X'), 0.0)
        self.assertEqual(park.debrid_park_remaining(None), 0.0)


if __name__ == '__main__':
    unittest.main()
