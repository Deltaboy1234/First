"""On-disk folders, recoverable deletion and collision-safe batch exports."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import string
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import imaging as im

EDITED = "Edited"
TRASH = "Fieldroom Trash"
MANIFEST = ".fieldroom.json"


def stamp(path):
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def private_child(parent, name):
    child = parent / name
    if child.is_symlink():
        raise ValueError(f"{child} is a symbolic link. Choose a regular folder for this operation.")
    child.mkdir(exist_ok=True)
    return child


def manifest(folder):
    path = folder / EDITED / MANIFEST
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("photos"), dict):
        raise ValueError(f"Could not read the saved edit settings in {path}.")
    return data["photos"]


def write_manifest(directory, entries):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False) as out:
        temp = Path(out.name)
        try:
            json.dump({"version": 1, "photos": entries}, out, ensure_ascii=True, indent=1)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            out.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, directory / MANIFEST)
    finally:
        temp.unlink(missing_ok=True)


@contextlib.contextmanager
def folder_lock(directory):
    path = directory / ".fieldroom.lock"
    if path.is_symlink():
        raise ValueError("The output lock is a symbolic link.")
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if not path.stat().st_size:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class Photo:
    path: Path
    revision: str

    @property
    def id(self):
        key = os.path.normcase(str(self.path)) + "\0" + self.revision
        return hashlib.sha256(os.fsencode(key)).hexdigest()[:28]

    def verify(self):
        if self.path.is_symlink() or not self.path.is_file() or stamp(self.path) != self.revision:
            raise ValueError(f"{self.path.name} has changed or moved. Reopen its folder.")
        return self


def reserve_name(folder, source):
    for index in range(10000):
        extra = "" if index == 0 else f" ({index + 1})"
        path = folder / f"{source.stem}{extra}.jpg"
        try:
            with path.open("xb"):
                return path
        except FileExistsError:
            continue
    raise ValueError(f"No available output name for {source.name}.")


def owned_output(directory, record):
    name = record.get("output", "")
    if not name or Path(name).name != name:
        return None
    path = directory / name
    if path.is_symlink() or not path.is_file():
        return None
    return path if stamp(path) == record.get("written") else None


def export(photo, values):
    source = photo.verify().path
    settings = im.recipe(values)
    directory = private_child(source.parent, EDITED)
    with folder_lock(directory):
        entries = manifest(source.parent)
        existing = entries.get(source.name, {})
        output = owned_output(directory, existing)
        if output and existing.get("source") == photo.revision and existing.get("recipe") == settings:
            return {"path": str(output), "skipped": True}
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as file:
            tmp = Path(file.name)
        reserved = None
        try:
            if source.suffix.lower() in (".jpg", ".jpeg") and settings == im.recipe():
                # Copy JPEG originals losslessly when exporting a completely untouched photo.
                shutil.copyfile(source, tmp)
            else:
                original = im.decode(source)
                result = im.render(original, settings)
                result.save(tmp, format="JPEG", quality=95, subsampling=0,
                            icc_profile=im.SRGB, exif=original.exif)
            photo.verify()
            # Recheck ownership in case an external editor changed the previous export.
            output = owned_output(directory, existing)
            if output is None:
                reserved = output = reserve_name(directory, source)
            os.replace(tmp, output)
            reserved = None
            entries[source.name] = {"source": photo.revision, "recipe": settings,
                                    "output": output.name, "written": stamp(output)}
            write_manifest(directory, entries)
            return {"path": str(output), "skipped": False}
        finally:
            tmp.unlink(missing_ok=True)
            if reserved is not None:
                reserved.unlink(missing_ok=True)


def places():
    home = Path.home()
    items = [(home / "Pictures", "Pictures"), (home, "Home")]
    if os.name == "nt":
        items += [(Path(f"{letter}:/"), f"{letter}: drive") for letter in string.ascii_uppercase if os.path.isdir(f"{letter}:/")]
    else:
        items += [(Path("/"), "Filesystem")]
        mounts = [Path("/media") / home.name, Path("/run/media") / home.name, Path("/media"), Path("/mnt"), Path("/Volumes")]
        if hasattr(os, "getuid"):
            mounts.append(Path("/run/user") / str(os.getuid()) / "gvfs")
        for base in mounts:
            if base.is_dir():
                items.append((base, str(base)))
                try:
                    items.extend((d, d.name) for d in sorted(base.iterdir()) if d.is_dir())
                except OSError:
                    pass
    unique = {}
    for path, label in items:
        if path.is_dir():
            unique.setdefault(str(path), {"path": str(path), "name": label})
    return list(unique.values())


class Session:
    def __init__(self):
        self.photos = {}
        self.deleted = {}
        self.lock = threading.RLock()
        self.demo_path = None

    def browse(self, location):
        path = Path(location).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("Choose a folder.")
        dirs, photos = [], []
        entries = manifest(path)
        for entry in sorted(path.iterdir(), key=lambda p: (p.name.casefold(), p.name)):
            if entry.name.startswith(".") or entry.name == TRASH:
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
                elif not entry.is_symlink() and entry.is_file() and entry.suffix.lower() in im.FORMATS:
                    photo = Photo(entry, stamp(entry))
                    self.photos[photo.id] = photo
                    prev = entries.get(entry.name, {})
                    saved = prev.get("source") == photo.revision
                    settings = im.recipe(prev.get("recipe") if saved else None)
                    current = bool(saved and owned_output(path / EDITED, prev))
                    photos.append({"id": photo.id, "name": entry.name, "folder": str(path),
                                   "recipe": settings, "exported": current})
            except (FileNotFoundError, PermissionError):
                continue
        return {"path": str(path), "name": path.name or str(path), "parent": str(path.parent),
                "folders": dirs, "photos": photos}

    def get(self, key):
        if key not in self.photos:
            raise ValueError("Open the photo folder again.")
        return self.photos[key].verify()

    def trash(self, key):
        with self.lock:
            photo = self.get(key)
            directory = private_child(photo.path.parent, TRASH)
            ticket = uuid.uuid4().hex
            slot = directory / ticket
            slot.mkdir()
            dest = slot / photo.path.name
            try:
                photo.path.rename(dest)
            except OSError:
                slot.rmdir()
                raise
            self.deleted[ticket] = (photo, dest)
            return {"ticket": ticket, "name": photo.path.name, "location": str(dest)}

    def undo(self, ticket):
        with self.lock:
            if ticket not in self.deleted:
                raise ValueError("This deletion can be restored manually from Fieldroom Trash.")
            photo, dest = self.deleted[ticket]
            # Reserve the old name atomically. Never overwrite a newly created source.
            try:
                with photo.path.open("xb"):
                    pass
            except FileExistsError:
                raise ValueError(f"{photo.path.name} is already in the folder. The deleted photo is in {dest.parent}.")
            try:
                os.replace(dest, photo.path)
            except OSError:
                photo.path.unlink(missing_ok=True)
                raise
            del self.deleted[ticket]
            with contextlib.suppress(OSError):
                dest.parent.rmdir()
            return {"id": photo.id, "folder": str(photo.path.parent), "name": photo.path.name}

    def demo(self):
        with self.lock:
            if self.demo_path is None:
                from .samples import make_demo
                self.demo_path = make_demo()
            return self.browse(self.demo_path)
