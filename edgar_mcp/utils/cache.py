"""File cache for SEC responses, stored as JSON.

Each entry is one JSON file named by the SHA-256 of its key. Entries are
JSON only: nothing read from disk is ever executed.

A read never removes anything. An expired entry is a miss and stays on disk
until a write replaces it: deleting it during a read could remove a fresh
entry another thread or process wrote in the meantime. Removal is explicit
(`delete`), or clearing the cache folder.

A read holds the file open only while it copies the bytes, and a write
retries its rename briefly, because Windows refuses to replace a file that is
open.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

from edgar_mcp.config.settings import CACHE_DIR, CACHE_ENABLED, CACHE_EXPIRY


class Cache:
    """A simple file-based cache of JSON-compatible values."""

    def __init__(self, namespace="default", expiry=CACHE_EXPIRY):
        """
        Args:
            namespace (str): Sub-directory for this cache to avoid key conflicts
            expiry (int): Default cache expiry time in seconds
        """
        self.namespace = namespace
        self.cache_dir = os.path.join(CACHE_DIR, namespace)
        self.enabled = CACHE_ENABLED
        self.default_expiry = expiry

    def _get_cache_path(self, key):
        digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, f"{digest}.json")

    def set(self, key, value, ttl=None, meta=None):
        """Store a value. `meta` (source URL, retrieval time) is kept beside it."""
        if not self.enabled:
            return
        entry = {
            'key': str(key),
            'expires_at': time.time() + (self.default_expiry if ttl is None else ttl),
            'meta': meta or {},
            'data': value,
        }
        # Best effort: a cache that cannot be written (read-only or full disk,
        # a value JSON cannot hold) must never fail the request it serves.
        temp_path = None
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            # Write to a temporary file and rename, so a reader never sees half a file.
            handle, temp_path = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
            with os.fdopen(handle, 'w', encoding='utf-8') as f:
                json.dump(entry, f)
            self._replace(temp_path, self._get_cache_path(key))
        except (OSError, TypeError, ValueError):
            try:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

    @staticmethod
    def _replace(source, target, tries=20, pause=0.05):
        for attempt in range(tries):
            try:
                os.replace(source, target)
                return
            except PermissionError:  # the target is open in another reader (Windows)
                if attempt == tries - 1:
                    raise
                time.sleep(pause)

    def get_entry(self, key):
        """The stored entry ({'data', 'meta', ...}), or None if absent, expired or unreadable.

        Never deletes: an expired or unreadable entry is a miss and is left for
        the next write to replace."""
        if not self.enabled:
            return None
        cache_path = self._get_cache_path(key)
        try:
            with open(cache_path, 'rb') as f:
                raw = f.read()
            entry = json.loads(raw.decode('utf-8'))
        except (OSError, ValueError):
            return None
        if (not isinstance(entry, dict) or 'data' not in entry
                or entry.get('key') != str(key)
                or not isinstance(entry.get('expires_at'), (int, float))):
            return None
        if time.time() > entry['expires_at']:
            return None
        return entry

    def get(self, key):
        """The cached value, or None if not found or expired."""
        entry = self.get_entry(key)
        return entry['data'] if entry else None

    def delete(self, key):
        """Remove an entry on purpose. Never called by a read."""
        if not self.enabled:
            return
        try:
            os.remove(self._get_cache_path(key))
        except OSError:
            pass
