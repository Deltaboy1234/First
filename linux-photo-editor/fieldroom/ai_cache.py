"""Best-effort persistent cache for full-resolution local AI results.

Only hashed, Fieldroom-owned .npy files are read or evicted. The caller's key
must include the original fingerprint, processing settings and model signature.
Committed arrays are limited to 1 GiB in total; an in-flight atomic write can
temporarily need additional space. Cache failure must never block photo edits.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from numbers import Integral
import threading

import numpy as np

try:
    import fcntl
except ImportError:  # The desktop target is Linux; other platforms skip caching.
    fcntl = None

MAX_CACHE_BYTES = 1024**3
_OWNED = re.compile(r"fieldroom-ai-[0-9a-f]{64}\.npy\Z")
_LOCK = threading.RLock()
_LOCK_NAME = ".fieldroom-ai-cache.lock"


def _filename(key):
    if not isinstance(key, tuple):
        raise ValueError("The AI cache key must be a tuple.")
    def encode(value):
        if isinstance(value, os.PathLike):
            return os.fspath(value)
        raise TypeError("Unsupported AI cache key value.")
    encoded = json.dumps(key, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":"), default=encode).encode("utf-8")
    return "fieldroom-ai-" + hashlib.sha256(encoded).hexdigest() + ".npy"


def _configured_path():
    override = os.environ.get("FIELDROOM_AI_CACHE_DIR")
    if override:
        path = Path(override).expanduser()
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home()/".cache").expanduser()
        path = base/"fieldroom"/"ai-detail"
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        return None
    return path


def _open_directory(create):
    """Walk using directory descriptors; never follow a symlink out of the cache."""
    descriptor = None
    try:
        path = _configured_path()
        if path is None or not hasattr(os, "O_NOFOLLOW"):
            return None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path.anchor, flags)
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass  # Another Fieldroom process may have created it.
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        return None


def _shape(expected_size):
    if not isinstance(expected_size, (tuple, list)) or len(expected_size) != 2 or any(
            isinstance(value, bool) or not isinstance(value, Integral) or value <= 0
            for value in expected_size):
        raise ValueError("Invalid AI cache dimensions.")
    width, height = map(int, expected_size)
    if width*height*3*2 > MAX_CACHE_BYTES:
        raise ValueError("The AI result is larger than the cache budget.")
    return height, width, 3


def _regular(descriptor):
    info = os.fstat(descriptor)
    # A hard link could share metadata with a file outside this cache directory.
    return info if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 else None


def _close_array(value):
    try:
        mapping = getattr(value, "_mmap", None)
        if mapping is not None:
            mapping.close()
        elif hasattr(value, "close"):
            value.close()
    except Exception:
        pass


def load(key, expected_size):
    """Return a read-only uint16 H×W×3 memmap, or None for any cache miss.

    Opening the .npy through its already-open Linux descriptor keeps header and
    data on the same inode even if another process atomically replaces the key.
    Integer uint16 data cannot contain NaN/Inf, so validation needs no full scan.
    """
    directory = descriptor = None
    value = None
    try:
        name, expected = _filename(key), _shape(expected_size)
        directory = _open_directory(False)
        if directory is None:
            return None
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        info = _regular(descriptor)
        if info is None or info.st_size > MAX_CACHE_BYTES:
            return None
        value = np.load(f"/proc/self/fd/{descriptor}", mmap_mode="r", allow_pickle=False)
        if (not isinstance(value, np.memmap) or value.dtype != np.dtype(np.uint16) or value.shape != expected
                or value.offset + value.nbytes != info.st_size):
            _close_array(value)
            value = None
            return None
        value.flags.writeable = False
        # mtime is the cache's explicit access timestamp; reading does not rely on
        # filesystem atime policies. A touch failure need not invalidate data.
        try:
            os.utime(descriptor, None)
        except OSError:
            pass
        return value
    except Exception:
        if value is not None:
            _close_array(value)
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def has(key, expected_size):
    """Validate a cached result without reading its pixel pages into memory."""
    value = load(key, expected_size)
    if value is None:
        return False
    _close_array(value)
    return True


def _make_room(directory, new_name, new_size):
    entries = []
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if entry.name == new_name or not _OWNED.fullmatch(entry.name):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                entries.append((info.st_mtime_ns, entry.name, info.st_size))
    total = new_size + sum(size for _, _, size in entries)
    for _, name, size in sorted(entries):
        if total <= MAX_CACHE_BYTES:
            break
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        except OSError:
            continue
        total -= size
    return total <= MAX_CACHE_BYTES


def save(key, pixels):
    """Atomically save a uint16 H×W×3 result; return False on any cache failure."""
    directory = lock_descriptor = None
    temporary = None
    try:
        name = _filename(key)
        if (not isinstance(pixels, np.ndarray) or pixels.dtype != np.dtype(np.uint16)
                or pixels.ndim != 3 or pixels.shape[2] != 3 or min(pixels.shape) <= 0
                or pixels.nbytes > MAX_CACHE_BYTES or fcntl is None):
            return False
        directory = _open_directory(True)
        if directory is None:
            return False
        temporary = "." + name + "." + secrets.token_hex(12) + ".tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            np.save(stream, pixels, allow_pickle=False)
            stream.flush()
            os.fsync(stream.fileno())
            new_size = os.fstat(stream.fileno()).st_size
        if new_size > MAX_CACHE_BYTES:
            return False
        # The write itself need not block readers/other temporary writers. Only
        # final eviction and replacement are serialized between threads/processes.
        with _LOCK:
            lock_descriptor = os.open(_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                      0o600, dir_fd=directory)
            if _regular(lock_descriptor) is None:
                return False
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            try:
                existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                    return False
            except FileNotFoundError:
                pass
            if not _make_room(directory, name, new_size):
                return False
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
            os.fsync(directory)
        return True
    except Exception:
        return False
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if temporary is not None and directory is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
        if directory is not None:
            os.close(directory)
