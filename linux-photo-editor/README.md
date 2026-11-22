# Fieldroom for Linux

Fieldroom opens a folder of photos in a Classic Develop workspace. The left panel holds the folder browser and Navigator. The filmstrip runs along the bottom. Basic adjustments, noise reduction and sharpening sit on the right. Image processing runs on your computer and the interface opens in your desktop browser.

## Install and start

From a Linux desktop with Python 3.10 or newer, Git and Python's venv module, run:

```bash
git clone https://github.com/Deltaboy1234/First.git
cd First/linux-photo-editor
bash install.sh
"$HOME/.local/bin/fieldroom"
```

The first install downloads Pillow, NumPy and rawpy. The installer creates a per-user environment and a **Fieldroom** entry in the applications menu. On Ubuntu, Debian or Linux Mint, install `python3-venv` if Python reports that venv or ensurepip is unavailable. For an existing checkout, pull the latest changes and rerun `bash install.sh`.

You can also download the repository ZIP while signed in to GitHub, extract it and run `bash install.sh` from its `linux-photo-editor` folder. Launch Fieldroom without a folder to try six disposable sample photos. To open a specific folder, run `"$HOME/.local/bin/fieldroom" "/path/to/photos"`. **Open folder** accepts a pasted path and lists mounted drives.

On Windows, double-click **Test-Fieldroom.cmd** for a trial. It uses an available Python environment, including the Codex photo libraries when present. A Python environment with Pillow and NumPy can test raster images. Camera RAW requires rawpy. With the standard Python launcher, the script can create a project-local virtual environment for the full editor.

## Inspect focus and noise

| Control | Action |
| --- | --- |
| Click the main image or tap **Space** | Toggle between Fit or Fill and your last detail magnification. The default detail view is 100%. Small samples that already fill the screen use the next higher step. |
| Scroll over the image | Zoom toward or away from the point under the pointer. |
| **+** / **−** or **Ctrl +** / **Ctrl −** | Step through zoom levels. **Ctrl+0** returns to Fit. |
| **Fit**, **Fill**, **1:1** or the percentage menu | Fit shows the whole photo. Fill covers the viewing area. Percentages range from 6.25% to 1600%. At 100%, one source pixel occupies one physical display pixel. |
| Drag the photo, or click and drag in the Navigator | Pan. The Navigator frame shows the visible part of the photo. |
| **Space** + drag or middle-button drag | Pan while the crop tool is active. |

The editor loads lossless tiles from the oriented, full-resolution source for detail inspection. Edits apply to those tiles. Switching photos retains the zoom level so you can compare focus across neighboring frames.

## Edit and save a folder

1. Click a filmstrip thumbnail or use **← / →** to browse. Adjust the sliders. Double-click a slider to reset it.
2. Press **R** to crop. Drag inside the original frame to draw a new crop. Once a smaller crop exists, drag inside it to move it or drag a corner to resize. **Enter** accepts. **Esc** cancels. The accepted crop stays with the photo as you browse.
3. **Delete** moves the source to **Fieldroom Trash** in its original folder. **Undo delete** or **Ctrl+Z** restores it during the session. The original filename is retained for manual recovery later.
4. **Save All** or **Ctrl+S** writes full-resolution JPEG copies to an **Edited** subfolder. It processes the open folder and any pending edits in other folders you visited. An untouched JPEG is copied without recompression. A camera RAW is developed into a JPEG. Source files are preserved.
5. Open **Edited** in the save results to inspect the finished files. Saved settings also reopen with the originals. Repeated saves skip current outputs. If you changed an output in another editor, Fieldroom chooses a fresh filename.

**Original** compares the original tones with the edited tones using the same crop and zoom. RAW decoding uses LibRaw through rawpy. Support depends on camera model and compression. A Nikon NEF recorded with an unsupported compression can appear in the filmstrip while development reports a decoder error.

## Project and photo credits

Layout A was selected in the [original design review](design-review/Fieldroom-Develop-Overview.jpg). The [interactive design picker](design-review/Fieldroom-Design-Picker.html) also remains available.

Dahlia is credited to Vultilion and the lakeside pavilion to Daniel Buechele. The bundled samples are resized JPEGs under Creative Commons Attribution 2.0. [Dahlia original](https://www.flickr.com/photos/vultilion/6056698931/) · [Pavilion original](https://www.flickr.com/photos/danielbuechele/6061409035/) · [License](https://creativecommons.org/licenses/by/2.0/)

Chelsea is by Stefan van der Walt, the espresso photograph by Rachel Michetti, and the photographer portrait by Lav Varshney. All three are CC0. The DSCOVR launch photograph is public domain, courtesy of SpaceX. [Sample image credits](https://scikit-image.org/docs/stable/api/skimage.data.html)
