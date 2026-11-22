"""Open the example photographs already distributed in the design review."""
import base64
import json
import re
import sysconfig
import tempfile
from pathlib import Path


PICKER = "Fieldroom-Design-Picker.html"
DEMO_NAMES = {
    "flower": "00-Dahlia.jpg",
    "rocket": "01-Cape-Canaveral.jpg",
    "coffee": "02-Espresso-Bar.jpg",
    "camera": "03-Photographer.jpg",
    "lake": "04-Lakeside-Pavilion.jpg",
    "cat": "05-Chelsea.jpg",
}


def make_demo():
    root = Path(__file__).resolve().parent.parent
    candidates = (root / "design-review" / PICKER, root / "share" / "fieldroom" / PICKER,
                  Path(sysconfig.get_path("data")) / "share" / "fieldroom" / PICKER)
    picker = next((path for path in candidates if path.is_file()), None)
    if picker is None:
        raise FileNotFoundError("Fieldroom's sample roll is missing. Reinstall or launch with a photo folder.")
    document = picker.read_text(encoding="utf-8")
    assets = re.search(r'<script id="assets" type="application/json">(.*?)</script>', document, re.S)
    if assets is None:
        raise ValueError("Fieldroom's sample roll could not be read.")
    photos = {item["key"]: item["url"] for item in json.loads(assets[1])}
    target = Path(tempfile.mkdtemp(prefix="fieldroom-")) / "Sample roll"
    target.mkdir()
    for key, filename in DEMO_NAMES.items():
        prefix = "data:image/jpeg;base64,"
        if not photos[key].startswith(prefix):
            raise ValueError(f"Invalid sample photograph: {filename}")
        (target / filename).write_bytes(base64.b64decode(photos[key][len(prefix):], validate=True))
    return target
