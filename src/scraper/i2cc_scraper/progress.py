"""Optional progress reporting shared by the crawler and downloaders.

Discovery records relationships even when a URL was already visited. Reporters must be
idempotent: a retried task may report the same event again. Storage errors propagate rather
than letting the workflow silently finish with missing progress.
"""
from typing import Protocol


class Progress(Protocol):
    def discovered(self, url: str, kind: str, *, parent: str = '', title: str = '',
                   context: str = '') -> None: ...

    def discovery(self, url: str, status: str, *, error: str = '', reason: str = '') -> None: ...

    def content(self, url: str, status: str, *, result: dict | None = None,
                error: str = '', reason: str = '') -> None: ...


class NullProgress:
    def discovered(self, url, kind, **details):
        pass

    def discovery(self, url, status, **details):
        pass

    def content(self, url, status, **details):
        pass


NULL_PROGRESS = NullProgress()
