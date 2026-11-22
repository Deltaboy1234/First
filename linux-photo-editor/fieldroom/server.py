"""A single-user, loopback-only desktop workspace."""
from __future__ import annotations

import base64
import contextlib
import hmac
import json
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from . import imaging as im
from .library import Session, export, places

WEB = Path(__file__).with_name("web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/style.css": ("style.css", "text/css"),
          "/editor.js": ("editor.js", "text/javascript"), "/viewport.js": ("viewport.js", "text/javascript"),
          "/icon.svg": ("icon.svg", "image/svg+xml")}


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, start=None):
        super().__init__(("127.0.0.1", port), Handler)
        self.key = secrets.token_urlsafe(30)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.session = Session()
        self.start_folder = start
        self.view_lock = threading.Semaphore(2)
        self.thumb_lock = threading.Semaphore(3)
        self.full_lock = threading.Lock()
        self.last_seen = time.monotonic()

    def dispatch(self, route, data):
        session = self.session
        if route == "boot":
            return {"places": places(), "defaults": im.recipe(), "raw": im.rawpy is not None,
                    "folder": session.browse(self.start_folder) if self.start_folder else session.demo()}
        if route == "folder":
            return session.browse(data["path"])
        if route == "directories":
            path = Path(data["path"]).expanduser().resolve(strict=True)
            folders = []
            for entry in sorted(path.iterdir(), key=lambda p: p.name.casefold()):
                if entry.name.startswith("."):
                    continue
                with contextlib.suppress(OSError):
                    if entry.is_dir():
                        folders.append({"name": entry.name, "path": str(entry)})
            return {"name": path.name or str(path), "path": str(path), "parent": str(path.parent), "folders": folders}
        if route == "demo":
            return session.demo()
        if route == "undo":
            return session.undo(data["ticket"])
        if route == "trash":
            return session.trash(data["id"])
        photo = session.get(data["id"])
        if route == "export":
            with session.lock:
                return export(photo, data["recipe"])
        if route == "smart_crop":
            from .smartcrop import suggest
            return suggest(photo.path, data.get("preset", "tight"),
                           subject_id=data.get("subject_id"), aspect=data.get("aspect", "Free"),
                           subject_ids=data.get("subject_ids"))
        if route == "auto_tone":
            source = im.decode(photo.path, 960)
            return {"recipe": im.auto_tone(source)}
        if route == "thumbnail":
            with self.thumb_lock:
                settings = im.recipe(data.get("recipe"))
                if photo.path.suffix.lower() in im.RAWS and settings != im.recipe():
                    source = im.decode(photo.path, 270)
                else:
                    source = im.decode(photo.path, 270, thumbnail=True)
                return im.jpeg(im.render(source, settings), 78)
        if route == "tile":
            with self.view_lock:
                return im.detail_tile(self.detail_source(photo), data.get("recipe"),
                                      int(data["column"]), int(data["row"]), int(data["step"]))
        if route != "preview":
            raise ValueError("Unknown action.")
        settings = im.recipe(data.get("recipe"))
        with self.view_lock:
            source = im.preview_source(photo.path, photo.revision, 1440)
            result = im.render(source, settings)
            return {"image": base64.b64encode(im.jpeg(result, 90)).decode("ascii"),
                    "histogram": im.histogram(result), "size": source.size, "metadata": source.metadata,
                    "box": im.pixel_box(source.size, settings["crop"]), "pixels": result.size}

    def detail_source(self, photo):
        # One full-size cache for panning at 1:1. Fit previews use a separate bounded cache.
        with self.full_lock:
            if getattr(self, "_detail_id", None) != photo.id:
                self._detail_source = im.decode(photo.path)
                self._detail_id = photo.id
            return self._detail_source

    def closed_tab(self):
        last = time.monotonic()
        def finish():
            if self.last_seen <= last:
                self.shutdown()
        timer = threading.Timer(12, finish)
        timer.daemon = True
        timer.start()


class Handler(BaseHTTPRequestHandler):
    server_version = f"Fieldroom/{__version__}"

    def log_message(self, *_):
        pass

    def reply(self, code, payload, mime="application/json"):
        if mime == "application/json":
            payload = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if mime.startswith("text/html"):
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.wfile.write(payload)

    def good_host(self):
        return self.headers.get("Host") == self.server.origin.split("//")[1]

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.good_host() or path not in STATIC:
            return self.reply(404, {"error": "Not found."})
        filename, mime = STATIC[path]
        self.reply(200, (WEB / filename).read_bytes(), mime)

    def do_POST(self):
        if not self.good_host() or self.headers.get("Origin", self.server.origin) != self.server.origin:
            return self.reply(403, {"error": "This local session is private."})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length < 65536 or not self.path.startswith("/api/"):
                return self.reply(400, {"error": "Invalid request."})
            data = json.loads(self.rfile.read(length))
            route = self.path[5:]
            secret = self.headers.get("X-Fieldroom-Key", "") or (data.get("key", "") if route == "closed" else "")
            if not isinstance(secret, str) or not hmac.compare_digest(secret, self.server.key):
                return self.reply(403, {"error": "Reopen Fieldroom using its launcher to start a session."})
            self.server.last_seen = time.monotonic()
            if route == "closed":
                self.server.closed_tab()
                return self.reply(200, {})
            if route == "alive":
                return self.reply(200, {})
            result = self.server.dispatch(route, data)
            mime = "image/png" if route == "tile" else "image/jpeg"
            self.reply(200, result, mime if isinstance(result, bytes) else "application/json")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            self.reply(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self.reply(500, {"error": str(exc) or "The image could not be processed."})
