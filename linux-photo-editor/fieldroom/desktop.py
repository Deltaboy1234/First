"""Standalone Qt desktop shell for the local Fieldroom editor.

All image processing and assets stay on this machine. The embedded Qt WebEngine
renders the existing Develop interface without launching an external browser.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import threading
import time

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(description="Fieldroom desktop editor")
    parser.add_argument("--version", action="version", version=f"Fieldroom {__version__}")
    parser.add_argument("folder", nargs="?", type=Path, help="initial photo folder")
    args = parser.parse_args()
    if args.folder and not args.folder.expanduser().is_dir():
        parser.error(f"Folder does not exist: {args.folder}")

    # Import Qt only when desktop mode is requested, leaving CLI/headless use intact.
    try:
        from PySide6.QtCore import QUrl, Qt, QTimer
        from PySide6.QtGui import QAction, QKeySequence
        from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError as exc:
        parser.error("Qt desktop dependencies missing. Install PySide6 and PySide6-Addons: " + str(exc))

    from .server import EditorServer

    os.environ.setdefault("OMP_NUM_THREADS", "4")
    app = QApplication([])
    app.setApplicationName("Fieldroom")
    app.setOrganizationName("Fieldroom")
    app.setQuitOnLastWindowClosed(True)

    folder = args.folder.expanduser().resolve() if args.folder else None
    with EditorServer(0, folder) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        class Window(QMainWindow):
            def __init__(self):
                super().__init__()
                self.setWindowTitle(f"Fieldroom {__version__} — Develop")
                self.resize(1500, 920)
                self.browser = QWebEngineView(self)
                self.browser.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
                self.setCentralWidget(self.browser)
                self.browser.setUrl(QUrl(server.origin + "/#" + server.key))
                self._allow_close = False
                self._closing_request = False
                self._close_poll_pending = False
                self._close_timer = QTimer(self)
                self._close_timer.setInterval(100)
                self._close_timer.timeout.connect(self._poll_close)
                self._add_actions()

            def closeEvent(self, event):
                if self._allow_close:
                    super().closeEvent(event)
                    return
                event.ignore()
                if self._closing_request:
                    return
                self._closing_request = True
                self._close_deadline = time.monotonic() + 30
                self.browser.page().runJavaScript("""
                    globalThis.__fieldroomCloseResult = null;
                    Promise.resolve().then(() =>
                        typeof globalThis.fieldroomPrepareClose === 'function'
                            ? globalThis.fieldroomPrepareClose() : true
                    ).then(ready => {
                        globalThis.__fieldroomCloseResult = ready ? 'ready' : 'cancelled';
                    }).catch(() => { globalThis.__fieldroomCloseResult = 'failed'; });
                """)
                self._close_timer.start()

            def _poll_close(self):
                if not self._closing_request:
                    return
                if time.monotonic() >= self._close_deadline:
                    self._close_result("failed")
                    return
                if self._close_poll_pending:
                    return
                self._close_poll_pending = True
                self.browser.page().runJavaScript("globalThis.__fieldroomCloseResult", self._close_result)

            def _close_result(self, result):
                self._close_poll_pending = False
                if not self._closing_request or result not in ("ready", "cancelled", "failed"):
                    return
                self._close_timer.stop()
                self._closing_request = False
                if result == "ready":
                    self._allow_close = True
                    self.close()
                elif result == "failed":
                    choice = QMessageBox.warning(
                        self, "Save edit recovery",
                        "Fieldroom could not confirm that your latest edits were saved. "
                        "Retry, keep the window open, or close without saving the latest changes.",
                        QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.Cancel)
                    if choice == QMessageBox.StandardButton.Retry:
                        self.close()
                    elif choice == QMessageBox.StandardButton.Discard:
                        self.browser.page().runJavaScript("globalThis.__fieldroomDiscardClose = true;")
                        self._allow_close = True
                        self.close()

            def _add_actions(self):
                refresh = QAction("Refresh workspace", self)
                refresh.setShortcut(QKeySequence("Ctrl+R"))
                refresh.triggered.connect(self.browser.reload)
                self.addAction(refresh)

                fullscreen = QAction("Toggle fullscreen", self)
                fullscreen.setShortcut(QKeySequence("F11"))
                fullscreen.triggered.connect(
                    lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen()
                )
                self.addAction(fullscreen)

                quit_action = QAction("Quit", self)
                quit_action.setShortcut(QKeySequence("Ctrl+Q"))
                quit_action.triggered.connect(self.close)
                self.addAction(quit_action)

        window = Window()
        window.show()
        try:
            app.exec()
        finally:
            server.shutdown()
            worker.join(timeout=3)


if __name__ == "__main__":
    main()
