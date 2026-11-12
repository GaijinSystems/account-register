"""In-memory sessions (single user); login throttling is in register.throttle. The password
itself is checked by the recorder: logging in means unlocking the encrypted data."""
import secrets
import time


class Sessions:
    """Server-side sessions: the cookie holds only a random token.

    Restarting the web process logs everyone out; logout really revokes.
    """

    def __init__(self, idle_seconds, max_seconds, clock=time.monotonic):
        self.idle, self.max, self.clock = idle_seconds, max_seconds, clock
        self._sessions = {}

    def create(self):
        self._purge()
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self._sessions[token] = {"csrf": secrets.token_urlsafe(32), "created": now, "seen": now}
        return token, self._sessions[token]

    def get(self, token):
        s = self._sessions.get(token) if token else None
        if s is None:
            return None
        now = self.clock()
        if self._expired(s, now):
            self._sessions.pop(token, None)
            return None
        s["seen"] = now
        return s

    def delete(self, token):
        self._sessions.pop(token, None)

    def clear(self):
        self._sessions.clear()

    def _expired(self, s, now):
        return now - s["seen"] > self.idle or now - s["created"] > self.max

    def _purge(self):
        now = self.clock()
        for t in [t for t, s in self._sessions.items() if self._expired(s, now)]:
            del self._sessions[t]
