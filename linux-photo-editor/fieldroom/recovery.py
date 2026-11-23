"""Durable accepted edits and the last workspace, independent of JPEG exports."""
from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
from pathlib import Path

from . import imaging as im


def state_directory():
    override = os.environ.get("FIELDROOM_STATE_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("FIELDROOM_STATE_DIR must be an absolute directory.")
        return path
    root = Path(os.environ.get("XDG_STATE_HOME", "")).expanduser()
    if not root.is_absolute():
        root = Path.home() / ".local" / "state"
    return root / "fieldroom"


class RecoveryStore:
    """SQLite transactions keep a failed or interrupted save from losing edits.

    Reading a new workspace does not create files. Tests and portable installs
    can select an isolated state directory through FIELDROOM_STATE_DIR.
    """

    def __init__(self, directory=None):
        self.path = Path(directory) / "recovery.sqlite3" if directory is not None else state_directory() / "recovery.sqlite3"
        self.lock = threading.RLock()
        self.warning = None

    def _connect(self, write=False):
        if self.warning:
            raise ValueError(self.warning)
        if not write and not self.path.exists():
            return None
        if self.path.is_symlink():
            raise ValueError("The recovery database is a symbolic link.")
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        connection = sqlite3.connect(str(self.path), timeout=5)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("The recovery database was created by a newer Fieldroom version.")
            if version == 0:
                if not write:
                    raise ValueError("The recovery database has no recognized format.")
                with connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS edits (path TEXT PRIMARY KEY, source TEXT NOT NULL, recipe TEXT NOT NULL)")
                    connection.execute("CREATE TABLE IF NOT EXISTS workspace (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), folder TEXT NOT NULL, photo TEXT)")
                    connection.execute("PRAGMA user_version = 1")
            return connection
        except BaseException:
            connection.close()
            raise

    def _read_error(self, error):
        if not self.warning:
            self.warning = f"Could not read automatic edit recovery. Existing recovery data was kept: {error}"

    def location(self):
        with self.lock:
            try:
                connection = self._connect()
                if connection is None:
                    return None
                with contextlib.closing(connection):
                    row = connection.execute("SELECT folder, photo FROM workspace WHERE singleton = 1").fetchone()
                    return {"path": row[0], "photo": row[1]} if row else None
            except (OSError, sqlite3.Error, ValueError) as exc:
                self._read_error(exc)
                return None

    def recipes(self, photos):
        result = {}
        with self.lock:
            try:
                connection = self._connect()
                if connection is None:
                    return result
                with contextlib.closing(connection):
                    for photo in photos:
                        row = connection.execute("SELECT source, recipe FROM edits WHERE path = ?", (str(photo.path),)).fetchone()
                        if row and row[0] == photo.revision:
                            values = json.loads(row[1])
                            if not isinstance(values, dict):
                                raise ValueError("An automatic edit record is invalid.")
                            result[photo.id] = im.recipe(values)
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                self._read_error(exc)
                return {}
        return result

    def save(self, edits, location=None):
        # Prepare the complete transaction before opening the database for writes.
        rows = [(str(photo.path), photo.revision, json.dumps(im.recipe(values), allow_nan=False, separators=(",", ":")))
                for photo, values in edits]
        with self.lock:
            try:
                connection = self._connect(write=True)
                with contextlib.closing(connection), connection:
                    connection.executemany(
                        "INSERT INTO edits (path, source, recipe) VALUES (?, ?, ?) "
                        "ON CONFLICT(path) DO UPDATE SET source = excluded.source, recipe = excluded.recipe", rows)
                    if location is not None:
                        connection.execute(
                            "INSERT INTO workspace (singleton, folder, photo) VALUES (1, ?, ?) "
                            "ON CONFLICT(singleton) DO UPDATE SET folder = excluded.folder, photo = excluded.photo", location)
            except sqlite3.Error as exc:
                raise ValueError(f"Could not save automatic edit recovery: {exc}") from exc


class RecoverySession:
    """Validate renderer snapshots against photos actually opened by this server."""

    def __init__(self, session, directory=None):
        self.session = session
        self.store = RecoveryStore(directory)
        self.lock = threading.RLock()
        self.client = None
        self.sequence = 0
        self.folders = set()
        self.fallback_demo = None

    def apply(self, folder):
        with self.lock:
            self.folders.add(folder["path"])
            photos = [self.session.photos[row["id"]] for row in folder["photos"]]
            recovered = self.store.recipes(photos)
            for row in folder["photos"]:
                row["saved_recipe"] = im.recipe(row["recipe"])
                row["recipe"] = recovered.get(row["id"], row["recipe"])
                row["recovered"] = row["recipe"] != row["saved_recipe"]
                row["exported"] = row["exported"] and not row["recovered"]
            return folder

    def browse(self, path):
        return self.apply(self.session.browse(path))

    def boot(self, start=None):
        with self.lock:
            self.client = secrets.token_urlsafe(24)
            self.sequence = 0
            self.fallback_demo = None
            saved = self.store.location()
            warning = None
            if start is not None:
                folder = self.browse(start)
            elif saved:
                try:
                    folder = self.browse(saved["path"])
                except (OSError, ValueError):
                    folder = self.apply(self.session.demo())
                    self.fallback_demo = folder["path"]
                    warning = "Your last folder is unavailable. Its saved edits are kept; reconnect the drive and open that folder to continue."
            else:
                folder = self.apply(self.session.demo())
            selected = None
            if saved and saved["path"] == folder["path"]:
                selected = next((row["id"] for row in folder["photos"]
                                 if str(self.session.photos[row["id"]].path) == saved["photo"]), None)
            return folder, {"client": self.client, "sequence": self.sequence, "selected_id": selected,
                            "restored_count": sum(bool(row["recovered"]) for row in folder["photos"]),
                            "warning": self.store.warning or warning}

    def save(self, data):
        with self.lock, self.session.lock:
            if not isinstance(data, dict):
                raise ValueError("Invalid recovery snapshot.")
            client, sequence = data.get("client"), data.get("sequence")
            if not isinstance(client, str) or not self.client or not hmac.compare_digest(client, self.client):
                return {"saved": False, "stale": True, "sequence": self.sequence}
            if type(sequence) is not int or not 0 < sequence <= 2**53 - 1:
                raise ValueError("Invalid recovery sequence.")
            if sequence <= self.sequence:
                return {"saved": False, "stale": True, "sequence": self.sequence}
            path, selected, changes = data.get("path"), data.get("id"), data.get("changes")
            if not isinstance(path, str) or path not in self.folders:
                raise ValueError("Open the folder before saving its workspace.")
            if not isinstance(changes, list) or len(changes) > 10000:
                raise ValueError("Invalid recovery edits.")
            selected_photo = self.session.photos.get(selected) if isinstance(selected, str) else None
            if selected is not None and (selected_photo is None or str(selected_photo.path.parent) != path):
                raise ValueError("The selected photo does not belong to this folder.")
            edits, seen = [], set()
            for item in changes:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("recipe"), dict):
                    raise ValueError("Invalid recovery edit.")
                identifier = item["id"]
                photo = self.session.photos.get(identifier)
                if photo is None or identifier in seen:
                    raise ValueError("The recovery snapshot includes an unknown or duplicate photo.")
                seen.add(identifier)
                values = im.recipe(item["recipe"])
                if photo.path.is_symlink():
                    raise ValueError(f"{photo.path.name} has changed. Reopen its folder.")
                try:
                    current = photo.path.stat()
                except OSError:
                    # A disconnected drive must not prevent recovery of its edits.
                    # The original fingerprint is checked again when it reappears.
                    current = None
                if current is not None and (not stat.S_ISREG(current.st_mode) or
                        f"{current.st_size}:{current.st_mtime_ns}" != photo.revision):
                    raise ValueError(f"{photo.path.name} has changed. Reopen its folder before saving its edits.")
                edits.append((photo, values))
            location = None if path == self.fallback_demo else (path, str(selected_photo.path) if selected_photo else None)
            self.store.save(edits, location)
            self.sequence = sequence
            return {"saved": True, "sequence": sequence}

    def exported(self, photo, values):
        # Export is already durable; keep recovery aligned with its accepted recipe.
        with self.lock:
            self.store.save([(photo, values)])
