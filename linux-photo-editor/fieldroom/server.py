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
from .recovery import RecoverySession
from .enhancement_jobs import EnhancementJobs

WEB = Path(__file__).with_name("web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/style.css": ("style.css", "text/css"),
          "/editor.js": ("editor.js", "text/javascript"), "/viewport.js": ("viewport.js", "text/javascript"),
          "/icon.svg": ("icon.svg", "image/svg+xml")}


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, start=None, state_directory=None):
        super().__init__(("127.0.0.1", port), Handler)
        self.key = secrets.token_urlsafe(30)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.session = Session()
        self.recovery = RecoverySession(self.session, state_directory)
        self.enhancements = EnhancementJobs()
        self.start_folder = start
        self.view_lock = threading.Semaphore(2)
        self.thumb_lock = threading.Semaphore(3)
        self.full_lock = threading.Lock()
        self.last_seen = time.monotonic()

    def dispatch(self, route, data):
        session = self.session
        if route == "boot":
            folder, recovery = self.recovery.boot(self.start_folder)
            try:
                from .enhance import status
                ai = status()
            except (ImportError, OSError, ValueError, RuntimeError) as exc:
                ai = {"available": False, "denoise": False, "sharpen": False,
                      "backend": "CPU", "message": str(exc) or "Offline AI detail is unavailable."}
            return {"places": places(), "defaults": im.recipe(), "raw": im.rawpy is not None,
                    "folder": folder, "recovery": recovery, "ai": ai}
        if route == "folder":
            return self.recovery.browse(data["path"])
        if route == "recovery":
            return self.recovery.save(data)
        if route == "ai_progress":
            return self.enhancements.progress(data.get("job"))
        if route == "ai_cancel":
            return self.enhancements.cancel(data.get("job"))
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
            return self.recovery.apply(session.demo())
        if route == "undo":
            return session.undo(data["ticket"])
        if route == "trash":
            return session.trash(data["id"])
        photo = session.get(data["id"])
        if route == "ai_prepare":
            return self.enhancements.start(photo, data.get("denoise", 0), data.get("sharpen", 0))
        if route == "export":
            with session.lock:
                result = export(photo, data["recipe"])
            try:
                self.recovery.exported(photo, data["recipe"])
            except (OSError, ValueError) as exc:
                # The JPEG and its recipe are already durable in the export
                # manifest. A separate recovery failure must not undo that save.
                result["recovery_warning"] = str(exc)
            return result
        if route == "smart_crop":
            from .smartcrop import suggest
            return suggest(photo.path, data.get("preset", "tight"),
                           subject_id=data.get("subject_id"), aspect=data.get("aspect", "Free"),
                           subject_ids=data.get("subject_ids"))
        if route in ("auto_tone", "auto_color"):
            from .auto_adjust import suggest
            return suggest(photo.path, "tone" if route == "auto_tone" else "color", data.get("subject_ids"), crop=data.get("crop"))
        if route == "thumbnail":
            with self.thumb_lock:
                settings = im.recipe(data.get("recipe"))
                if photo.path.suffix.lower() in im.RAWS and settings != im.recipe():
                    source = im.decode(photo.path, 270)
                else:
                    source = im.decode(photo.path, 270, thumbnail=True)
                prepared = im.prepared_ai_source(source, settings)
                if prepared is None:
                    # Thumbnails must not launch minutes of neural processing
                    # while the user simply scrolls through a large folder.
                    settings = {**settings, "ai_denoise": 0, "ai_sharpen": 0}
                else:
                    source = prepared
                return im.jpeg(im.render(source, settings), 78)
        if route == "tile":
            with self.view_lock:
                settings = im.recipe(data.get("recipe"))
                source = im.prepared_ai_source(self.detail_source(photo), settings, required=True)
                return im.detail_tile(source, settings,
                                      int(data["column"]), int(data["row"]), int(data["step"]))
        if route != "preview":
            raise ValueError("Unknown action.")
        settings = im.recipe(data.get("recipe"))
        with self.view_lock:
            source = im.preview_source(photo.path, photo.revision, 1440)
            source = im.prepared_ai_source(source, settings, required=True)
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

    def server_close(self):
        if hasattr(self, "enhancements"):
            self.enhancements.close()
        super().server_close()


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
            route = self.path[5:] if self.path.startswith("/api/") else ""
            limit = 8 * 1024 * 1024 if route in ("recovery", "closed") else 65536
            if not 0 < length < limit or not route:
                return self.reply(400, {"error": "Invalid request."})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Invalid request.")
            secret = self.headers.get("X-Fieldroom-Key", "") or (data.get("key", "") if route == "closed" else "")
            if not isinstance(secret, str) or not hmac.compare_digest(secret, self.server.key):
                return self.reply(403, {"error": "Reopen Fieldroom using its launcher to start a session."})
            self.server.last_seen = time.monotonic()
            if route == "closed":
                if "recovery" in data:
                    self.server.recovery.save(data["recovery"])
                self.server.closed_tab()
                return self.reply(200, {})
            if route == "alive":
                return self.reply(200, {})
            result = self.server.dispatch(route, data)
            mime = "image/png" if route == "tile" else "image/jpeg"
            self.reply(200, result, mime if isinstance(result, bytes) else "application/json")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            self.reply(400, {"error": str(exc), **({"code": exc.code} if hasattr(exc, "code") else {})})
        except Exception as exc:
            traceback.print_exc()
            self.reply(500, {"error": str(exc) or "The image could not be processed."})
