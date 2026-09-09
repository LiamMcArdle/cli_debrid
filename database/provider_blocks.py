"""Hashes a debrid provider has refused, keyed on (hash, provider), with the reason.

Real-Debrid answers HTTP 451 to addMagnet/addTorrent for a hash on its takedown
list and keeps answering it: of 150 such hashes measured on 2026-09-08, none
ever succeeded before or after, while other hashes added fine in the same
second. That is a fact about the hash *on that provider*. Recording it in
not_wanted_magnets -- global, permanent, reason-less -- would block the same
hash on a fallback provider that might serve it, and made a provider refusal
indistinguishable from a broken torrent. not_wanted keeps the latter job.

The scrape-time filter drops a result only when every configured provider has
refused it. The Adding queue counts refusals so an item whose every candidate
was refused is held as 'provider_blocked' instead of walking the retry ladder.
"""

import logging
import re
import threading
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set

from database.core import get_db_connection

_CACHE: Dict[str, Set[str]] = {}
_CACHE_LOADED_AT = 0.0
_CACHE_TTL_SECONDS = 60.0
_LOCK = threading.Lock()
_BTIH = re.compile(r'btih:([0-9a-fA-F]{40})')

CREATE_SQL = '''
    CREATE TABLE IF NOT EXISTS provider_blocks (
        hash TEXT NOT NULL,
        provider TEXT NOT NULL,
        reason TEXT,
        error_code INTEGER,
        blocked_at TEXT NOT NULL,
        PRIMARY KEY (hash, provider)
    )
'''


def ensure_table(conn=None) -> None:
    own = conn is None
    if own:
        conn = get_db_connection()
    try:
        conn.execute(CREATE_SQL)
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def _load(force: bool = False) -> Dict[str, Set[str]]:
    global _CACHE, _CACHE_LOADED_AT
    now = time.time()
    with _LOCK:
        if not force and _CACHE_LOADED_AT and now - _CACHE_LOADED_AT < _CACHE_TTL_SECONDS:
            return _CACHE
    fresh: Dict[str, Set[str]] = {}
    conn = get_db_connection()
    try:
        ensure_table(conn)
        for row in conn.execute('SELECT hash, provider FROM provider_blocks'):
            fresh.setdefault(row[0], set()).add(row[1])
    finally:
        conn.close()
    with _LOCK:
        _CACHE, _CACHE_LOADED_AT = fresh, now
    return fresh


def record_block(hash_value: Optional[str], provider: Optional[str],
                 reason: Optional[str] = None, error_code: Optional[int] = None) -> bool:
    """Remember that ``provider`` refused ``hash_value``. Returns True when new."""
    if not hash_value or not provider:
        return False
    h = hash_value.lower()
    conn = get_db_connection()
    try:
        ensure_table(conn)
        cur = conn.execute(
            'INSERT OR IGNORE INTO provider_blocks (hash, provider, reason, error_code, blocked_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (h, provider, reason, error_code, datetime.now().isoformat(timespec='seconds')))
        conn.commit()
        is_new = cur.rowcount > 0
    finally:
        conn.close()
    with _LOCK:
        _CACHE.setdefault(h, set()).add(provider)
    if is_new:
        code = f", error_code={error_code}" if error_code is not None else ""
        logging.warning(
            f"{provider} refuses hash {h[:16]}… ({reason or 'blocked'}{code}); "
            f"it will not be offered to {provider} again."
        )
    return is_new


def blocked_providers(hash_value: Optional[str]) -> Set[str]:
    if not hash_value:
        return set()
    return set(_load().get(hash_value.lower(), ()))


def is_blocked(hash_value: Optional[str], provider: str) -> bool:
    return provider in blocked_providers(hash_value)


def is_blocked_everywhere(hash_value: Optional[str], providers: Iterable[str]) -> bool:
    """True when every configured provider has refused the hash -- only then is
    there nowhere left to send it."""
    names = {p for p in providers if p}
    if not names or not hash_value:
        return False
    return names <= blocked_providers(hash_value)


def hash_from_magnet(magnet: Optional[str]) -> Optional[str]:
    if not magnet:
        return None
    m = _BTIH.search(magnet)
    return m.group(1).lower() if m else None


def configured_provider_names() -> List[str]:
    try:
        from debrid import get_debrid_providers
        return [p.PROVIDER_NAME for p in get_debrid_providers()]
    except Exception:
        return []


def is_result_blocked_everywhere(magnet: Optional[str]) -> bool:
    """Scrape-time filter: drop a result every configured provider has refused."""
    h = hash_from_magnet(magnet)
    if not h:
        return False
    return is_blocked_everywhere(h, configured_provider_names())
