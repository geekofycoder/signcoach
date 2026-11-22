"""Who may start a session, and how many at once. These keep a public URL from running up the API bill."""

import secrets


def passcode_ok(supplied: str | None, expected: str | None) -> bool:
    """True when no passcode is required, or the supplied one matches."""
    if not expected:
        return True
    return secrets.compare_digest((supplied or "").encode(), expected.encode())


class SessionCounter:
    """Counts running sessions. A limit of 0 or less means no limit."""

    def __init__(self, limit: int):
        self.limit = limit
        self.active = 0

    def full(self) -> bool:
        return self.limit > 0 and self.active >= self.limit

    def acquire(self):
        self.active += 1

    def release(self):
        self.active = max(0, self.active - 1)
