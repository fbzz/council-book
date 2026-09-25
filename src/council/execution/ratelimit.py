"""Client-side limiter for broker writes (open, close, patch share one budget).

Rule: at most `capacity` acquisitions in ANY window of `per_seconds` (default 18 per 60 s: headroom
under eToro's shared 20/60 s execution pool, and a full flatten still fits). It is a strict sliding
window, not a refilling bucket: a classic bucket of 18 refilling at 18/60 s would allow up to 36
sends inside one 60 s window and break the broker's quota. A broker 429 can additionally `pause`
every sender for the server's Retry-After.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

DEFAULT_CAPACITY = 18
DEFAULT_PER_SECONDS = 60.0


class TokenBucket:
    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        per_seconds: float = DEFAULT_PER_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if capacity < 1 or per_seconds <= 0:
            raise ValueError("capacity must be >= 1 and per_seconds > 0")
        self.capacity = capacity
        self.per_seconds = float(per_seconds)
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self._paused_until = float("-inf")

    def _prune(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= self.per_seconds:
            self._sent.popleft()

    def wait_time(self) -> float:
        """Seconds until the next acquisition is allowed (0 = now)."""
        now = self._clock()
        self._prune(now)
        wait = max(0.0, self._paused_until - now)
        if len(self._sent) >= self.capacity:
            wait = max(wait, self._sent[0] + self.per_seconds - now)
        return wait

    def try_acquire(self) -> bool:
        if self.wait_time() > 0:
            return False
        self._sent.append(self._clock())
        return True

    def acquire(self) -> float:
        """Block until a send is allowed, record it, and return the seconds waited."""
        waited = 0.0
        while True:
            wait = self.wait_time()
            if wait <= 0:
                self._sent.append(self._clock())
                return waited
            self._sleep(wait)
            waited += wait

    def pause(self, seconds: float) -> None:
        """Hold every sender for `seconds` (a broker Retry-After)."""
        self._paused_until = max(self._paused_until, self._clock() + max(0.0, seconds))
