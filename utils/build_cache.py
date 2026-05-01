"""On-disk pickle cache for expensive game-data builds.

Several tabs (Item Catalog, Dialogue Catalog, Audio) re-parse hundreds
of MB of localization + game-data files every time they open. On a
full game install that takes 30-90 seconds — long enough that users
think the lazy-load system is broken. This module gives those builds
a transparent disk cache:

  * Cache key = a fingerprint of the inputs (size + mtime of each
    PAMT/PAR file the build reads).
  * If the fingerprint matches the previous run, we deserialize the
    pickled result and skip the rebuild entirely (~100 ms vs 60 s).
  * If anything changed (game updated, new install, file edited),
    the fingerprint mismatches and we rebuild + re-cache.

The cache lives in ~/.crimsonforge/cache/ alongside the user's
settings file. It's safe to delete — the next open just rebuilds.
Pickle is intentional: dataclasses with slots round-trip cleanly,
and pickle is ~10× faster than JSON for large nested structures.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import struct
import tempfile
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache directory
# ---------------------------------------------------------------------------
def cache_dir() -> Path:
    """Return the directory used for build caches.

    Defaults to ``~/.crimsonforge/cache``. Created on demand.
    """
    base = Path.home() / ".crimsonforge" / "cache"
    base.mkdir(parents=True, exist_ok=True)
    return base


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------
def fingerprint_paths(paths: Iterable[str | os.PathLike]) -> str:
    """Build a short fingerprint string from the size+mtime of each file.

    We deliberately avoid hashing file contents — that would defeat
    the purpose of the cache. Size+mtime is what every modern build
    system uses (make, ninja, esbuild) and is good enough here:
    Steam updates the file's mtime when it patches.
    """
    h = hashlib.sha256()
    for p in paths:
        try:
            st = os.stat(p)
            # Encode as fixed-width binary so order matters but the
            # human-readable representation doesn't influence the hash.
            h.update(str(p).encode("utf-8", errors="replace"))
            h.update(b"\x00")
            h.update(struct.pack("<qq", st.st_size, int(st.st_mtime_ns)))
        except OSError:
            # Missing file → still hash the path so the fingerprint
            # changes once the file appears on disk.
            h.update(str(p).encode("utf-8", errors="replace"))
            h.update(b"\xff")
    return h.hexdigest()[:32]


def fingerprint_strings(parts: Iterable[str]) -> str:
    """Fingerprint from arbitrary string inputs (e.g. game version)."""
    h = hashlib.sha256()
    for s in parts:
        h.update(s.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------
def _cache_file(name: str) -> Path:
    # Names are short identifiers (e.g. "dialogue_catalog"); sanitize
    # to be safe against accidental path injection.
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return cache_dir() / f"{safe}.pkl"


def load_cached(name: str, fingerprint: str) -> Any | None:
    """Load a cached build if its fingerprint matches.

    Returns the cached payload, or ``None`` if no cache exists or the
    fingerprint differs (i.e. the input changed). All errors are
    swallowed and logged — a corrupt cache must never block the app.
    """
    path = _cache_file(name)
    if not path.is_file():
        return None
    try:
        with path.open("rb") as f:
            blob = pickle.load(f)
    except (pickle.UnpicklingError, EOFError, OSError, AttributeError, TypeError) as e:
        logger.warning("build_cache: drop corrupt cache %s (%s)", path.name, e)
        try:
            path.unlink()
        except OSError:
            pass
        return None
    if not isinstance(blob, dict) or blob.get("fingerprint") != fingerprint:
        return None
    if blob.get("version") != _CACHE_VERSION:
        return None
    return blob.get("payload")


def save_cached(name: str, fingerprint: str, payload: Any) -> None:
    """Atomically write a cache entry. Failures are logged, never raised."""
    path = _cache_file(name)
    blob = {
        "version": _CACHE_VERSION,
        "fingerprint": fingerprint,
        "payload": payload,
    }
    try:
        # Write to a temp file in the same directory then atomically
        # rename — guarantees the cache file is never half-written
        # if the process is killed mid-write.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.stem}_", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "wb") as f:
                pickle.dump(blob, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.warning("build_cache: failed to write %s (%s)", path.name, e)


def invalidate(name: str) -> None:
    """Force-remove a single cached entry (no-op if missing)."""
    try:
        _cache_file(name).unlink(missing_ok=True)
    except OSError:
        pass


def invalidate_all() -> None:
    """Wipe every entry in the cache directory."""
    base = cache_dir()
    for entry in base.glob("*.pkl"):
        try:
            entry.unlink()
        except OSError:
            pass


# Bump this when the on-disk pickle layout changes incompatibly so
# old caches are silently rejected instead of raising at unpickle.
_CACHE_VERSION = 1
