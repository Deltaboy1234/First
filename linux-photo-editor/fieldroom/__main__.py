#!/usr/bin/env python3
"""Launch Fieldroom and its private local processing service."""
import argparse
import os
from pathlib import Path
import threading
import webbrowser


def main():
    parser = argparse.ArgumentParser(description="Fieldroom photo editor")
    parser.add_argument("folder", nargs="?", type=Path, help="initial photo folder (defaults to disposable sample photos)")
    parser.add_argument("--no-browser", action="store_true", help="print the local address without opening a window")
    parser.add_argument("--port", type=int, default=0, help="local port (default: an available port)")
    args = parser.parse_args()
    if args.folder and not args.folder.expanduser().is_dir():
        parser.error(f"Folder does not exist: {args.folder}")
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    from .server import EditorServer
    with EditorServer(args.port, args.folder.expanduser().resolve() if args.folder else None) as server:
        address = server.origin + "/#" + server.key
        print(f"Fieldroom is ready. Open {address}", flush=True)
        if not args.no_browser:
            threading.Timer(.25, webbrowser.open, args=(address,)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
