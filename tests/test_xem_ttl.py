"""XEM: a definitive 'no show' is held for a month, a failed fetch for hours.

fetch_xem_mapping used to return None for both, and the battery re-fetched
every empty entry after six hours -- from inside get_show_metadata's cache-hit
path, for every show without a mapping, on every scrape cycle.
"""

import json
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from contextlib import contextmanager
from datetime import datetime, timezone

from cli_battery.app import xem_utils
from cli_battery.app import direct_api
from cli_battery.app.direct_api import (_xem_ttl_for, _xem_fetch_plan, _xem_fetch_and_store,
                                        _XEM_PROVIDER, _XEM_UNAVAILABLE_PROVIDER)


def _response(payload=None, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload or {}
    resp.raise_for_status = MagicMock()
    return resp


class FetchContract(unittest.TestCase):
    def setUp(self):
        xem_utils._xem_blocked_until = 0.0
        self.addCleanup(lambda: setattr(xem_utils, '_xem_blocked_until', 0.0))

    def test_success_returns_the_mapping_list(self):
        mapping = [{'scene': {'season': 1}, 'tvdb': {'season': 2}}]
        with patch('cli_battery.app.xem_utils.requests.get',
                   return_value=_response({'result': 'success', 'data': mapping})):
            self.assertEqual(xem_utils.fetch_xem_mapping(123), mapping)

    def test_no_show_is_a_definitive_empty_list(self):
        with patch('cli_battery.app.xem_utils.requests.get',
                   return_value=_response({'result': 'failure', 'message': 'no show with the tvdb id 123 found'})):
            self.assertEqual(xem_utils.fetch_xem_mapping(123), [])

    def test_other_failure_message_is_none(self):
        with patch('cli_battery.app.xem_utils.requests.get',
                   return_value=_response({'result': 'failure', 'message': 'backend down'})):
            self.assertIsNone(xem_utils.fetch_xem_mapping(123))

    def test_403_is_none_and_opens_the_cooldown(self):
        with patch('cli_battery.app.xem_utils.requests.get', return_value=_response(status=403)):
            self.assertIsNone(xem_utils.fetch_xem_mapping(123))
        self.assertGreater(xem_utils._xem_blocked_until, 0)
        with patch('cli_battery.app.xem_utils.requests.get') as get:
            self.assertIsNone(xem_utils.fetch_xem_mapping(456))
            get.assert_not_called()

    def test_timeout_is_none(self):
        with patch('cli_battery.app.xem_utils.requests.get',
                   side_effect=requests.exceptions.Timeout()):
            self.assertIsNone(xem_utils.fetch_xem_mapping(123))


def _session_with_row(row, titles_row='fetched'):
    """A session whose xem_mapping lookup returns `row`.

    The season_titles lookup returns a row already marked as having had its
    XEM names fetched, so the mapping logic under test is isolated; pass
    titles_row=None to exercise the names fetch.
    """
    session = MagicMock()
    if titles_row == 'fetched':
        titles_row = MagicMock(provider='provider,xem', value='{}')

    def filter_by(**kwargs):
        q = MagicMock()
        q.first.return_value = titles_row if kwargs.get('key') == 'season_titles' else row
        return q
    session.query.return_value.filter_by.side_effect = filter_by
    return session


def _row(provider, age):
    row = MagicMock()
    row.provider = provider
    row.last_updated = datetime.now(timezone.utc) - age
    return row


class FetchPlan(unittest.TestCase):
    def setUp(self):
        self.item = MagicMock(id=7)

    def test_populated_mapping_never_refetches(self):
        md = {'xem_mapping': [{'scene': {}}], 'ids': {'tvdb': 1}}
        self.assertIsNone(_xem_fetch_plan(self.item, _session_with_row(None), md))

    def test_missing_entry_is_fetched(self):
        md = {'ids': {'tvdb': 1}}
        self.assertEqual(_xem_fetch_plan(self.item, _session_with_row(None), md), (7, 1, True, False))

    def test_names_are_fetched_once_even_with_a_populated_mapping(self):
        md = {'xem_mapping': [{'scene': {}}], 'ids': {'tvdb': 1}}
        plan = _xem_fetch_plan(self.item, _session_with_row(None, titles_row=None), md)
        self.assertEqual(plan, (7, 1, False, True))
        already = MagicMock(provider='provider,xem', value='{}')
        self.assertIsNone(_xem_fetch_plan(self.item, _session_with_row(None, titles_row=already), md))

    def test_no_tvdb_id_means_nothing_to_ask(self):
        self.assertIsNone(_xem_fetch_plan(self.item, _session_with_row(None), {'ids': {}}))

    def test_fresh_definitive_empty_is_not_refetched(self):
        md = {'xem_mapping': [], 'ids': {'tvdb': 1}}
        session = _session_with_row(_row(_XEM_PROVIDER, timedelta(days=7)))
        self.assertIsNone(_xem_fetch_plan(self.item, session, md))

    def test_old_definitive_empty_is_refetched(self):
        md = {'xem_mapping': [], 'ids': {'tvdb': 1}}
        session = _session_with_row(_row(_XEM_PROVIDER, timedelta(days=31)))
        self.assertEqual(_xem_fetch_plan(self.item, session, md), (7, 1, True, False))

    def test_unavailable_placeholder_is_refetched_after_hours(self):
        md = {'xem_mapping': [], 'ids': {'tvdb': 1}}
        self.assertIsNone(_xem_fetch_plan(
            self.item, _session_with_row(_row(_XEM_UNAVAILABLE_PROVIDER, timedelta(hours=5))), md))
        self.assertEqual(_xem_fetch_plan(
            self.item, _session_with_row(_row(_XEM_UNAVAILABLE_PROVIDER, timedelta(hours=7))), md), (7, 1, True, False))


class FetchAndStore(unittest.TestCase):
    def _run(self, fetched, existing_row, md, plan=(7, 1, True, False), names=None, titles_row='fetched'):
        session = _session_with_row(existing_row, titles_row=titles_row)

        @contextmanager
        def fake_session():
            yield session

        with patch.object(direct_api, 'fetch_xem_mapping', return_value=fetched), \
                patch.object(direct_api, 'fetch_xem_names', return_value=names), \
                patch.object(direct_api, 'managed_session', fake_session):
            _xem_fetch_and_store(plan, md)
        return session

    def test_names_are_merged_into_season_titles_and_marked(self):
        titles_row = MagicMock(provider='provider', value=json.dumps({'17': ['Thousand-Year Blood War']}))
        self._run(None, None, {}, plan=(7, 1, False, True),
                  names={'17': ['Bleach - Sennen Kessen-hen']}, titles_row=titles_row)
        self.assertEqual(json.loads(titles_row.value),
                         {'17': ['Thousand-Year Blood War', 'Bleach - Sennen Kessen-hen']})
        self.assertEqual(titles_row.provider, 'provider,xem')

    def test_no_show_names_answer_still_marks_the_fetch_done(self):
        session = self._run(None, None, {}, plan=(7, 1, False, True), names={}, titles_row=None)
        session.add.assert_called_once()
        self.assertEqual(session.add.call_args.args[0].provider, 'xem')

    def test_failed_names_fetch_leaves_no_marker(self):
        session = self._run(None, None, {}, plan=(7, 1, False, True), names=None, titles_row=None)
        session.add.assert_not_called()

    def test_definitive_answer_overwrites_and_is_marked_xem(self):
        row = _row(_XEM_UNAVAILABLE_PROVIDER, timedelta(hours=7))
        md = {'xem_mapping': []}
        self._run([], row, md)
        self.assertEqual(row.provider, _XEM_PROVIDER)
        self.assertEqual(md['xem_mapping'], [])

    def test_mapping_is_stored_and_exposed(self):
        mapping = [{'scene': {'season': 1}}]
        md = {}
        session = self._run(mapping, None, md)
        self.assertEqual(md['xem_mapping'], mapping)
        session.add.assert_called_once()
        self.assertEqual(session.add.call_args.args[0].provider, _XEM_PROVIDER)

    def test_transient_failure_never_overwrites_an_existing_row(self):
        row = _row(_XEM_PROVIDER, timedelta(days=31))
        md = {'xem_mapping': []}
        session = self._run(None, row, md)
        self.assertEqual(row.provider, _XEM_PROVIDER)
        session.add.assert_not_called()

    def test_transient_failure_leaves_a_short_lived_placeholder_when_no_row(self):
        md = {}
        session = self._run(None, None, md)
        session.add.assert_called_once()
        self.assertEqual(session.add.call_args.args[0].provider, _XEM_UNAVAILABLE_PROVIDER)
        self.assertEqual(md['xem_mapping'], [])

    def test_no_plan_is_a_no_op(self):
        with patch.object(direct_api, 'fetch_xem_mapping') as fetch:
            _xem_fetch_and_store(None, {})
        fetch.assert_not_called()


class EmptyEntryTtl(unittest.TestCase):
    def test_definitive_empty_is_held_for_a_month(self):
        self.assertEqual(_xem_ttl_for(_XEM_PROVIDER), timedelta(days=30))
        self.assertEqual(_xem_ttl_for(None), timedelta(days=30))

    def test_unavailable_placeholder_is_retried_in_hours(self):
        self.assertEqual(_xem_ttl_for(_XEM_UNAVAILABLE_PROVIDER), timedelta(hours=6))


if __name__ == '__main__':
    unittest.main()
