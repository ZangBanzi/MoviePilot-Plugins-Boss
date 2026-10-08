"""Thread-safe LRU cache with a budget for encoded image bytes."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import MutableMapping


def _image_bytes(value):
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, (tuple, list)):
        return sum(_image_bytes(part) for part in value)
    return 0


class ImageCache(MutableMapping):
    """Never retain oversized entries; TTL is measured from insertion, not access."""

    def __init__(self, max_entries=96, max_bytes=64 * 1024 * 1024, ttl=None):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.ttl = ttl
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    def _remove(self, key):
        _, size, value = self._entries.pop(key)
        self._bytes -= size
        return value

    def _expire(self):
        now = time.monotonic()
        for key, (expires, _, _) in list(self._entries.items()):
            if expires is not None and now >= expires:
                self._remove(key)

    @property
    def byte_size(self):
        with self._lock:
            self._expire()
            return self._bytes

    def __getitem__(self, key):
        with self._lock:
            expires, _, value = self._entries[key]
            if expires is not None and time.monotonic() >= expires:
                self._remove(key)
                raise KeyError(key)
            self._entries.move_to_end(key)
            return value

    def __setitem__(self, key, value):
        size = _image_bytes(value)
        with self._lock:
            self._expire()
            if key in self._entries:
                self._remove(key)
            if size > self.max_bytes or self.max_entries < 1:
                return
            while self._entries and (len(self._entries) >= self.max_entries or
                                     self._bytes + size > self.max_bytes):
                self._remove(next(iter(self._entries)))
            expires = time.monotonic() + self.ttl if self.ttl is not None else None
            self._entries[key] = (expires, size, value)
            self._bytes += size

    def __delitem__(self, key):
        with self._lock:
            self._remove(key)

    def __iter__(self):
        with self._lock:
            self._expire()
            return iter(tuple(self._entries))

    def __len__(self):
        with self._lock:
            self._expire()
            return len(self._entries)

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0
