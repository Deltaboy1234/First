"""Standalone Qt desktop shell for the local Fieldroom editor.

All image processing and assets stay on this machine. The embedded Qt WebEngine
renders the existing Develop interface without launching an external browser.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import threading

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
        from PySide6.QtCore import QUrl, Qt
        from PySide6.QtGui import QAction, QKeySequence
        from PySide6.QtWidgets import QApplication, QMainWindow, QFileDialog
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
                self._add_actions()

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
