"""Bundle the shared desktop UI and publicly sourced photographs into one offline HTML file."""
import argparse
import base64
import hashlib
import html as html_lib
import io
import json
from pathlib import Path
import re
from urllib.request import urlopen

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "fieldroom" / "web"
EXAMPLES = (
    ("hubble_deep_field.jpg", "50c1b81aea1d565d78741387b5279bbed0ba4ce8", "01-Hubble-Deep-Field.jpg", "NASA, ESA and the HUDF teams · via scikit-image"),
    ("astronaut.png", "834cda0012478c5edc8d43bade96d315dedeaab4", "02-Eileen-Collins.jpg", "NASA · via scikit-image"),
    ("moon.png", "6a6f077e78df2b0e759f14e68bae31c8ad62f06f", "03-Lunar-Surface.jpg", "Lunar surface · via scikit-image"),
)
DECODER_ROOT = "https://raw.githubusercontent.com/radiantlab/LumiLab/462249fe5947567e7c934bdb539da5748712bc8e/"
DECODER = {
    "public/wasm/dcraw_emu.wasm": "d115b272493ee42f6a82dfde1f758e2585d994c3",
    "public/wasm/dcraw_emu.js": "0f22de070d6fd8ee11e0f7ba5634dd3c93f5a85e",
    "licenses/LGPL-2.1.txt": "4e964d9844d11d5eb95e3c6a16c1d4fb52f0bffa",
}


def verified_bytes(name, expected, cache, url):
    source = cache / name if cache else None
    if source and source.is_file():
        data = source.read_bytes()
    else:
        with urlopen(url, timeout=45) as response:
            data = response.read()
    if hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != expected:
        raise ValueError(f"The contents of the pinned dependency {name} have changed.")
    return data


def load_example(name, expected, cache):
    url = "https://raw.githubusercontent.com/scikit-image/scikit-image/v0.21.0/skimage/data/" + name
    data = verified_bytes(name, expected, cache, url)
    with Image.open(io.BytesIO(data)) as image:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=92, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def build(output, cache):
    read = lambda path: path.read_text(encoding="utf-8")
    samples = [{"name": title, "credit": credit, "url": load_example(name, sha, cache)}
               for name, sha, title, credit in EXAMPLES]
    html = read(WEB / "index.html")
    svg = "data:image/svg+xml;base64," + base64.b64encode((WEB / "icon.svg").read_bytes()).decode()
    css = read(WEB / "style.css") + """
    #trialFiles{max-width:450px}
    #trialFiles p{color:var(--muted);line-height:1.5}
    #trialFiles footer{justify-content:flex-start;flex-wrap:wrap}
    """
    html = html.replace("/icon.svg", svg).replace('<link rel="stylesheet" href="/style.css">', "<style>"+css+"</style>")
    html = html.replace('<script type="module" src="/editor.js"></script>', """
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: blob:; worker-src blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline' 'wasm-unsafe-eval'; connect-src 'none'; base-uri 'none'; form-action 'none'">""")
    html = html.replace("Fieldroom · Develop", "Fieldroom · RAW browser trial")
    html = html.replace("Open a photo folder (Ctrl+O)", "Choose photos or a folder (Ctrl+O)").replace("</svg>Open folder", "</svg>Open photos")
    html = html.replace("Save full-resolution copies to Edited (Ctrl+S)", "Prepare edited JPEGs to download (Ctrl+S)")
    html = html.replace("Move to Fieldroom Trash (Delete)", "Remove from this trial (Delete)").replace("Move photo to trash", "Remove photo from trial")
    html = html.replace("Delete Trash", "Delete Remove").replace("<h2>Save results</h2>", "<h2>Download edited photos</h2>")
    dialog = """<dialog id="trialFiles"><header><h2>Open photos</h2><button id="dismissImport" aria-label="Close">×</button></header>
      <p id="importNote">Choose photos on this device.</p>
      <input id="trialPhotos" type="file" accept=".dng,.nef,.jpg,.jpeg,.png,.webp,.bmp,.avif" multiple hidden>
      <input id="trialFolder" type="file" webkitdirectory multiple hidden>
      <footer><button id="pickPhotos" class="primary">Choose photos</button><button id="pickFolder">Choose a folder</button></footer></dialog>"""
    viewport, count = re.subn(r"\s*export\s*\{\s*PhotoViewport\s*\};\s*$", "", read(WEB / "viewport.js"))
    if count != 1:
        raise ValueError("Unexpected viewport module format.")
    editor, count = re.subn(r'^\s*import\s*\{\s*PhotoViewport\s*\}\s*from\s*"\./viewport\.js";', "", read(WEB / "editor.js"))
    if count != 1:
        raise ValueError("Unexpected editor module format.")
    decoder = {Path(path).name: verified_bytes(Path(path).name, sha, cache, DECODER_ROOT + path)
               for path, sha in DECODER.items()}
    glue = decoder["dcraw_emu.js"].decode()
    # A classic Blob worker also runs when the user double-clicks a file:// HTML.
    # The compiled WASM goes over postMessage, so the generated glue never fetches a module URL.
    glue = glue.replace("import.meta.url", "self.location.href").replace("export default Module;", "")
    worker = glue + "\n" + read(Path(__file__).with_name("raw-worker.js"))
    wasm = base64.b64encode(decoder["dcraw_emu.wasm"]).decode("ascii")
    notice = html_lib.escape(decoder["LGPL-2.1.txt"].decode())
    dialog = dialog.replace("</dialog>", """<details><summary>RAW decoder credits</summary><p>LibRaw dcraw_emu, copyright LibRaw LLC and contributors.
      <a href="https://github.com/radiantlab/LibRaw/tree/c9d6743ab87feb7cbe970675099cb64ee9abda19" target="_blank" rel="noreferrer">Decoder source and build instructions</a></p>
      <pre style="white-space:pre-wrap;max-height:180px;overflow:auto">""" + notice + "</pre></details></dialog>")
    adapter = read(Path(__file__).with_name("adapter.js"))
    script = "\n".join((viewport, read(Path(__file__).with_name("raw.js")), adapter, editor))
    if "</script" in (worker + script).lower():
        raise ValueError("Unsafe closing tag in bundled JavaScript.")
    components = '<script id="rawWasm" type="application/octet-stream">' + wasm + '</script><script id="rawWorker" type="text/plain">' + worker + "</script>"
    html = html.replace("</body>", dialog + components + '<script id="trialSamples" type="application/json">' +
                        json.dumps(samples, ensure_ascii=True) + '</script><script type="module">' + script + "</script></body>")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--asset-cache", type=Path, help="Directory of hash-verified, upstream scikit-image example files")
    args = parser.parse_args()
    build(args.output, args.asset_cache)
