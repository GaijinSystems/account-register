"""Exponential backoff after repeated wrong passwords, shared by the web process (logins)
and the recorder (unlocks). Standard library only, so both layers can import it."""
import time


class Throttle:
    """Global exponential backoff after repeated wrong passwords (single user)."""

    FREE_ATTEMPTS = 5
    MAX_DELAY = 300

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.failures = 0
        self.locked_until = 0.0

    def wait_seconds(self):
        return max(0, int(self.locked_until - self.clock() + 0.999))

    def fail(self):
        self.failures += 1
        if self.failures >= self.FREE_ATTEMPTS:
            delay = min(2 ** (self.failures - self.FREE_ATTEMPTS), self.MAX_DELAY)
            self.locked_until = self.clock() + delay

    def success(self):
        self.failures = 0
        self.locked_until = 0.0
