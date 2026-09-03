"""Process-wide cooldowns for scrapers that rate-limit by IP.

Nyaa sits behind DDoS-Guard and Torrentio behind its own limiter; both block the
whole host rather than throttling a single request, so once tripped EVERY
request fails, including a plain curl. The per-scrape `unavailable` set is
thread-local and only suppresses duplicate attempts inside one scrape, so each
new item re-attempted the blocked scraper and the traffic did nothing but hold
the block open (measured 2026-08-26: 203 of 203 Nyaa requests failing at ~20/min).

A park is therefore process-wide and shared by every queue worker. Callers still
get ScraperUnavailable — specifically ScraperParked, which the manager records
without counting a circuit-breaker timeout, because a skip we chose is not
evidence that the scraper is broken.

The queue reads `longest_park_remaining()` so a retry hold can outlast the park
instead of waking every held item into the same block thirty minutes later.
"""

import logging
import threading
import time
from typing import Dict, Optional, Tuple


class ScraperPark:
    """One scraper's cooldown. Doubles on consecutive trips, capped."""

    def __init__(self, name: str, base_seconds: float, max_seconds: float):
        self.name = name
        self.base_seconds = float(base_seconds)
        self.max_seconds = float(max_seconds)
        self._lock = threading.Lock()
        self._blocked_until = 0.0
        self._cooldown = float(base_seconds)

    def remaining(self) -> float:
        """Seconds left on the cooldown, 0 when the scraper may be queried."""
        with self._lock:
            return max(0.0, self._blocked_until - time.time())

    def trip(self) -> float:
        """Park the scraper after a rate limit. Returns the park length."""
        with self._lock:
            now = time.time()
            if now < self._blocked_until:
                # Already parked; a racing thread tripped it first.
                return self._blocked_until - now
            if self._blocked_until and now - self._blocked_until < self._cooldown:
                # Tripped again shortly after the last cooldown expired — back
                # off harder.
                self._cooldown = min(self._cooldown * 2, self.max_seconds)
            else:
                self._cooldown = self.base_seconds
            self._blocked_until = now + self._cooldown
            return self._cooldown

    def clear(self) -> None:
        """A successful request means the block lifted; reset the escalation."""
        if self._blocked_until or self._cooldown != self.base_seconds:
            with self._lock:
                self._blocked_until = 0.0
                self._cooldown = self.base_seconds


# Base/max are per-scraper because the blocks differ in kind: DDoS-Guard holds a
# Nyaa block for many minutes regardless of what we do, while Torrentio's limiter
# releases quickly once traffic stops.
PARKS: Dict[str, ScraperPark] = {
    'Nyaa': ScraperPark('Nyaa', 900, 7200),        # 15 min, doubling to 2h
    'Torrentio': ScraperPark('Torrentio', 120, 1800),  # 2 min, doubling to 30 min
}


def get_park(name: str) -> Optional[ScraperPark]:
    return PARKS.get(name)


def park_remaining(name: str) -> float:
    """Seconds left on one scraper's park, 0 when it is not parked."""
    park = PARKS.get(name)
    return park.remaining() if park else 0.0


def longest_park_remaining() -> Tuple[Optional[str], float]:
    """The scraper parked longest and its remaining seconds, else (None, 0)."""
    worst_name, worst_seconds = None, 0.0
    for name, park in PARKS.items():
        seconds = park.remaining()
        if seconds > worst_seconds:
            worst_name, worst_seconds = name, seconds
    return worst_name, worst_seconds


def trip(name: str, reason: str = 'rate limited') -> float:
    """Park a scraper and log it once. Returns the park length in seconds."""
    park = PARKS.get(name)
    if park is None:
        return 0.0
    seconds = park.trip()
    logging.warning(
        f"{name} {reason}. Parking {name} for {seconds / 60:.0f} min — the block "
        f"is by IP, so further requests only hold it open."
    )
    return seconds


def clear(name: str) -> None:
    park = PARKS.get(name)
    if park is not None:
        park.clear()
