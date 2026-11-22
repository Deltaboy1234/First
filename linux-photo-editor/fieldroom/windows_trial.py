"""Start the Windows trial with a user-owned RAW decoder for a bundled Python."""
import importlib
import os
from pathlib import Path
import subprocess
import sys

from .__main__ import main


def launch():
    if "-h" in sys.argv or "--help" in sys.argv:
        return main()
    root = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "Fieldroom" / sys.implementation.cache_tag
    sys.path.insert(0, str(root))
    try:
        importlib.import_module("rawpy")
    except (ImportError, OSError):
        print("Installing the camera RAW decoder for this Windows account. The first install needs internet access.", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "--only-binary=:all:", "--no-deps", "--upgrade", "--target", str(root),
                        "rawpy>=0.25.1"], check=True)
        importlib.invalidate_caches()
        importlib.import_module("rawpy")
    return main()


if __name__ == "__main__":
    launch()
