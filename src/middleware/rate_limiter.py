"""Token bucket rate limiter."""

from collections import deque
from time import time
from typing import Optional
import logging
import threading

from ..exceptions import RateLimitError


class RateLimiter:
    """Sliding-window rate limiter for controlling request rates.

    Thread-safe: a single lock guards the request timestamp deque so the
    SSE transport + asyncio.gather paths don't race when mutating the
    window boundaries.
    """

    def __init__(self, requests_per_minute: int):
        self.requests_per_minute = requests_per_minute
        self.requests = deque()
        self.window_seconds = 60
        self.logger = logging.getLogger(__name__)
        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        """Check if request is allowed under rate limit."""
        now = time()
        window_start = now - self.window_seconds

        with self._lock:
            # Remove old requests outside the window
            while self.requests and self.requests[0] < window_start:
                self.requests.popleft()

            # Check if we're under the limit
            if len(self.requests) < self.requests_per_minute:
                self.requests.append(now)
                return True

            over_limit_count = len(self.requests)

        self.logger.warning(f"Rate limit exceeded: {over_limit_count} requests in last minute")
        return False

    def check_and_raise(self) -> None:
        """Check rate limit and raise exception if exceeded."""
        if not self.is_allowed():
            raise RateLimitError(
                f"Rate limit exceeded: maximum {self.requests_per_minute} requests per minute"
            )

    def seconds_until_slot(self) -> float:
        """Seconds until the oldest request leaves the window (0 if free)."""
        now = time()
        with self._lock:
            while self.requests and self.requests[0] < now - self.window_seconds:
                self.requests.popleft()
            if len(self.requests) < self.requests_per_minute:
                return 0.0
            return max(0.05, self.requests[0] + self.window_seconds - now)

    async def acquire(self, max_wait: float = 60.0, sleep=None) -> None:
        """Wait (backoff) for a slot instead of failing; raise after max_wait.

        Bursty callers (e.g. one search per mailbox across 10 mailboxes)
        are paced to the configured rate rather than erroring out.
        """
        import asyncio
        sleep = sleep or asyncio.sleep
        waited = 0.0
        while not self.is_allowed():
            delay = self.seconds_until_slot()
            if waited + delay > max_wait:
                raise RateLimitError(
                    f"Rate limit exceeded: maximum {self.requests_per_minute} "
                    f"requests per minute (waited {waited:.0f}s)"
                )
            await sleep(delay)
            waited += delay

    def get_remaining(self) -> int:
        """Get remaining requests in current window."""
        now = time()
        window_start = now - self.window_seconds

        with self._lock:
            while self.requests and self.requests[0] < window_start:
                self.requests.popleft()
            return max(0, self.requests_per_minute - len(self.requests))

    def reset(self) -> None:
        """Reset the rate limiter."""
        with self._lock:
            self.requests.clear()
        self.logger.info("Rate limiter reset")
