# Fieldroom for Linux

Fieldroom 0.4.0 opens a folder of photos in its own Linux desktop window. The left panel holds the folder browser and Navigator, the filmstrip runs along the bottom, and the editing controls sit on the right. This release adds Undo/Redo, automatic edit recovery, a Before / After slider, Color controls with Auto Color, and offline AI Noise & Sharpen. Auto Tone and Auto Color consider the main person or the people you selected in Smart Crop.

The desktop application uses a **Qt window with an embedded local web interface rendered by Qt WebEngine**. It does not open your normal browser, but it is not a rewrite using native Qt widgets. Its Python service listens only on the local loopback interface. After installation downloads the dependencies and approximately **39 MB of verified model weights**, editing, RAW development, Smart Crop, automatic adjustments, and AI detail processing work without an internet connection or an AI account. Photos are not sent to a cloud service. The two AI detail models add **2.92 MB** to the existing 36.1 MB of face/person detection models.

## Install the Linux desktop application

You need Python 3.10 or newer, Git, and Python's venv module. On Pop!_OS, Ubuntu, Debian, or Linux Mint, install `python3-venv` if Python reports that venv or ensurepip is unavailable.

For a new checkout:

```bash
git clone --branch feature/fieldroom-linux-desktop --single-branch https://github.com/Deltaboy1234/First.git
cd First/linux-photo-editor
bash install.sh
```

For an existing checkout at `~/First`, close Fieldroom and update it with:

```bash
cd "$HOME/First"
git fetch origin
git switch feature/fieldroom-linux-desktop
git pull --ff-only origin feature/fieldroom-linux-desktop
cd linux-photo-editor
bash install.sh
```

The installer creates a per-user environment, installs the tested OpenCV and ONNX Runtime versions, verifies all four model downloads by exact size and SHA-256 checksum, and runs inference checks. Valid models are reused when you run the installer again; image processing never downloads models. It adds **Fieldroom** to the applications menu. Open that entry, or launch it directly:

```bash
"$HOME/.local/share/fieldroom/venv/bin/fieldroom-desktop"
```

To open a mounted drive or another specific folder, pass its quoted path:

```bash
"$HOME/.local/share/fieldroom/venv/bin/fieldroom-desktop" "/media/delta/Samsung USB"
```

The application also accepts folder paths in **Open folder**. Starting without a folder restores your last folder and selected photo when the folder is available. The first launch opens disposable sample photographs. If the last folder is on a disconnected drive, Fieldroom shows samples and keeps the saved edits so you can reconnect the drive and reopen the folder. Passing a folder on the command line opens that folder instead. The installer creates `~/.local/bin/fieldroom-desktop` and a compatibility `fieldroom` launcher when those names are available; it preserves unrelated existing files. If you use `XDG_DATA_HOME`, the environment lives under that directory instead of `~/.local/share`.

You can also download a ZIP of the **feature/fieldroom-linux-desktop** branch while signed in to GitHub, extract it, and run `bash install.sh` from its `linux-photo-editor` folder. A ZIP checkout must be updated with another ZIP rather than `git pull`.

## Smart Crop: preview before applying

1. Open a photograph, then click **Crop** or press **R**.
2. Choose **Very Tight**, **Tight**, **Normal**, **Loose**, or **Full Body**. Tight is the default portrait framing.
3. Click **Preview AI Crop**. The detector runs locally on the complete, oriented photograph, before any existing crop.
4. Review the proposed rectangle. Under **Include in crop**, use the checkboxes to add or remove people, or choose **All people**. Click a person's name to select only that person and clear the other selections. The preview updates as you change the selection. You can also change the preset, move the rectangle, or resize its corners.
5. Click **Apply Crop** or press **Enter** to accept the preview. **Cancel** or **Esc** leaves the accepted crop unchanged. Save the edited JPEG when you are ready.

An individual portrait preset needs a confidently detected face. If no face is detected reliably, Fieldroom keeps your current crop and explains the problem; it does not guess a center crop or invent a head position from a body box. Try **Full Body** when the person is visible but their face is turned away, or crop manually. Full Body uses the whole detected person and includes breathing room; it preserves the visible body but cannot restore feet, hands, or a head that were outside the original photo. Detection can still miss a subject, particularly with occlusion, unusual poses, or a small face, so always review the preview.

For multiple selected people, portrait framing surrounds all selected faces with headroom. If a selected person's face is unclear but their body is detected, the group preview includes their visible body and explains why the framing is wider. Full Body requires a detected body outline for every selected person. Aspect ratios expand the frame to keep the selected people; if a ratio cannot fit them, Fieldroom uses Free and explains the constraint. Unchecking everyone leaves your crop alone until you choose someone. A crop is a rectangle around the selection, so an unselected person standing between selected people may still appear inside it.

YuNet face detection and YOLOX person detection replace the earlier Haar/HOG implementation. The models are verified by exact size and SHA-256 checksum. Inference never downloads a model, and a missing or damaged model produces a setup error instead of falling back to the old detector.

## Basic, Color, and automatic adjustments

**Basic** contains Exposure, Contrast, Highlights, Shadows, Whites, and Blacks. **Color** sits immediately below Basic and contains **Auto Color**, Warmth, Tint, Vibrance, and Saturation. Warmth shifts the red/blue balance; Tint adjusts green/magenta. Vibrance favors less saturated colors and reduces its effect on likely warm skin colors. Saturation adjusts overall color intensity. Double-click a slider to reset it.

**Auto Tone** and **Auto Color** use the same local face/person detections as Smart Crop. Choose people with the Smart Crop checkboxes, accept or leave crop mode, then run an automatic adjustment. The current person selection is used while it remains in this editing session. With no selection, Fieldroom chooses the main prominent detected person within the accepted crop. The accepted crop limits the areas measured, and a selected person outside that crop produces an explanation instead of silently switching to someone else.

Auto Tone prioritizes visible face regions and gives each selected person a say. If a selected face is unavailable, it measures that person's detected body more cautiously. It considers bright areas across the accepted frame before increasing exposure. Its corrections are restrained: it does not force every complexion to the same brightness. You can refine the suggested values with the sliders or undo the adjustment.

Auto Color looks for consistent, nearly neutral areas near the selected people, excluding detected faces, likely skin, and strongly colored areas. When it finds enough evidence, it makes a small, bounded Warmth/Tint correction. **Without a reliable neutral reference, it leaves your existing white balance unchanged and explains why.** It does not treat skin or a colored shirt as a gray card, and it does not automatically increase saturation or vibrance.

If detection completes without finding a suitable person in the crop, the automatic controls explicitly report a restrained whole-image fallback. Missing models and invalid person selections produce an error rather than an undisclosed fallback. Automatic adjustments keep your accepted crop and unrelated edits.

## Offline AI Noise & Sharpen

Open **AI Noise & Sharpen**, below Color. Set **AI Noise Reduction** and **AI Sharpen** from 0 to 100, then click **Apply AI Detail**. Zero turns that model off. Moving these sliders creates a pending choice; the accepted edit changes only after processing succeeds. A progress bar and **Cancel processing** control remain available while the models run. Cancellation or a model error keeps the previous accepted edit.

AI Noise Reduction uses a pretrained **DnCNN** neural network. AI Sharpen uses a pretrained **ESPCN** network for modest learned detail enhancement: its luminance prediction is returned to the source dimensions and blended according to the strength. The photograph keeps its original pixel dimensions. These are actual local model runs, separate from the conventional Noise reduction and Sharpening panels that remain available below them.

Processing uses the CPU at the original resolution before cropping or preview resizing. **Large RAW photographs can take several minutes.** Examine the finished result at 100% and use Before / After or Undo to judge it; stronger denoising can soften fine textures, and AI Sharpen does not guarantee repair of missed focus or motion blur. Accepted AI detail is shared by the preview, full-resolution zoom tiles, and JPEG export. Returning to a photo with accepted AI settings prepares its result with progress and cancellation if it is no longer cached.

Fieldroom keeps up to **1 GiB of completed AI results** in `~/.cache/fieldroom/ai-detail` (or `$XDG_CACHE_HOME/fieldroom/ai-detail`). Matching results can be reused after Undo/Redo, navigation, or another launch, so the same accepted settings need not be processed again. The least recently used results are removed as needed; writing a result can temporarily require additional space. A changed original, AI strengths, or model version requires a matching new result. Tone, color, and crop changes can reuse the prepared AI source. `FIELDROOM_AI_CACHE_DIR` selects another absolute cache directory. The cache is disposable: an unavailable or corrupt entry is recomputed, and cache write failures do not discard an accepted edit.

## Check or prepare an offline installation

The default model directory is `~/.local/share/fieldroom/models` (or `$XDG_DATA_HOME/fieldroom/models`). Check both the face/person detectors and the AI detail models without opening a window or using the network:

```bash
"$HOME/.local/share/fieldroom/venv/bin/python" -m fieldroom.models --check
"$HOME/.local/share/fieldroom/venv/bin/python" -m fieldroom.enhance_models --check
```

To repair a model download while online:

```bash
"$HOME/.local/share/fieldroom/venv/bin/python" -m fieldroom.models --download
"$HOME/.local/share/fieldroom/venv/bin/python" -m fieldroom.enhance_models --download
```

For a machine that will stay offline, install the application and dependencies while connected first, or transfer compatible dependency wheels separately. Copy all four verified `.onnx` files to its model directory: the YuNet and YOLOX detection models, `dncnn_color.onnx`, and `super-resolution-10.onnx`. `FIELDROOM_MODEL_DIR` can point the application to a portable model folder; both command-line checkers and the installer accept that setting. Both model tools support `--directory /path/to/models` for an explicit location. They reuse valid files, verify downloads before replacing them, and remove incomplete downloads after a failure.

## Desktop view controls

**F** toggles Photo Only view; **F** or **Esc** returns to the editor. **F11** toggles the desktop window's full-screen mode. **← / →** moves between photographs when the comparison divider is not focused.

### Before / After with the same framing

Click **Before / After** beside **Original**, then drag the vertical divider. The left side shows the original tones and detail; the right side shows the accepted edit. Both sides use the **same accepted crop, zoom, and pan**, including full-resolution detail tiles when zoomed in. This makes it possible to compare exposure, color, noise, and sharpness without the subject shifting position. Comparison is hidden while you are choosing a crop and resumes after you apply or cancel that crop preview.

The divider is keyboard accessible: focus it with Tab and use **← / →** for small steps, **Shift + ← / →** for larger steps, or **Home / End** to show only After or Before. These keys move the focused divider without changing photographs. **Original** remains available to toggle the whole view between original and edited appearance with the same crop and zoom.

## Undo, Redo, and automatic recovery

The toolbar's **Undo** and **Redo** buttons work on the current photo's accepted adjustments, automatic adjustments, crop, reset, and AI detail. **Ctrl+Z** undoes an edit; **Ctrl+Shift+Z** or **Ctrl+Y** redoes it. One continuous slider drag is one Undo step. Histories are kept separately for each photo during the current application session; the history itself is not restored after closing.

Accepted edit settings are saved automatically, together with the last folder and selected photo. The status bar reports **Saving edits…**, **Edits saved**, or a save error. An unfinished crop preview and unapplied AI strengths are not included. Normal desktop closing waits for the latest recovery save and asks you to finish or cancel an active crop or AI job. If saving fails, the window offers retry, keeping it open, or explicitly closing without the latest changes.

**Automatic recovery saves editing settings. Save All exports the JPEG files.** You can close and reopen Fieldroom to continue accepted edits without exporting first. Recovery is stored separately from the photos in `~/.local/state/fieldroom` (or `$XDG_STATE_HOME/fieldroom`); `FIELDROOM_STATE_DIR` can select another absolute directory. Recovery checks that an original still matches the saved file before applying its settings. A disconnected drive's saved edits remain available when the drive returns.

## Try it in a browser first

The portable **Fieldroom-Browser-Trial.html** opens directly in a current desktop browser. Download the **Fieldroom-Browser-Trial** artifact from a successful [Fieldroom Linux test](https://github.com/Deltaboy1234/First/actions/workflows/fieldroom-linux.yml) and extract the HTML file. Drop in a **DNG**, **NEF** or **JPEG**, or start with the three sample photographs. The trial uses the same zoom, Navigator, filmstrip and crop controls as the desktop editor.

In the browser trial, **Delete** removes a photo from the current selection. **Save All → Download ZIP** downloads full-size edited JPEGs. **File information** identifies each successful sensor decode. If a camera's compression cannot be decoded and the file contains a usable JPEG preview, the trial labels it **Embedded preview only** and reports that file separately at export. The desktop edition adds disk folders, recoverable deletion and persistent edit settings. The browser trial uses its own image adjustments and keeps your selected files in the tab.

To rebuild the portable trial from source, run `python3 browser-trial/build.py Fieldroom-Browser-Trial.html` with Pillow installed. It bundles three scikit-image samples and a single-threaded, hash-verified WebAssembly build of **LibRaw dcraw_emu**. One background worker develops a RAW at a time. The resulting HTML works offline. The decoder notice and source are under **Open photos → RAW decoder credits**. [Matching LibRaw source and build system](https://github.com/radiantlab/LibRaw/tree/c9d6743ab87feb7cbe970675099cb64ee9abda19)

The browser trial is a separate demonstration and does not include desktop Smart Crop, subject-aware Auto Tone/Auto Color, offline AI Noise & Sharpen, or durable desktop recovery. The `fieldroom` Python entry point still opens the local-browser mode for development; the desktop launcher is `fieldroom-desktop`.

On Windows, **Test-Fieldroom.cmd** remains a separate browser-mode trial. It uses an available Python environment and may install the RAW decoder on its first run.

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

1. Scroll the mouse wheel over the bottom filmstrip to move the thumbnails sideways without changing the active photo or crop preview. Horizontal trackpad scrolling works too. Click a thumbnail or use **← / →** to select another photo. Adjust the sliders. Double-click a slider to reset it.
2. Press **R** to crop. Drag inside the original frame to draw a new crop. Once a smaller crop exists, drag inside it to move it or drag a corner to resize. **Enter** accepts. **Esc** cancels. The accepted crop stays with the photo as you browse.
3. **Delete** moves the source to **Fieldroom Trash** in its original folder. The explicit **Undo delete** button restores it during the session. **Ctrl+Z** first undoes an edit on the current photo; only when there is no edit to undo does it fall back to Undo delete. The original filename is retained for manual recovery later.
4. **Save All** or **Ctrl+S** writes full-resolution JPEG copies to an **Edited** subfolder. It processes the open folder and any pending edits in other folders you visited. An untouched JPEG is copied without recompression. A camera RAW is developed into a JPEG. Source files are preserved.
5. Open **Edited** in the save results to inspect the finished files. Saved settings also reopen with the originals. Repeated saves skip current outputs. If you changed an output in another editor, Fieldroom chooses a fresh filename.

RAW decoding uses LibRaw through rawpy. Support depends on camera model and compression. A Nikon NEF recorded with an unsupported compression can appear in the filmstrip while development reports a decoder error.

## Project and photo credits

### Offline detection models

Both model files are from [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7), pinned to commit `48b10fa505aeb51fbd127fc60a5c660040e823b7`:

| Model | Purpose | License | Size |
| --- | --- | --- | ---: |
| [YuNet 2023mar](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_detection_yunet) | Face location and confidence | [MIT; copyright Shiqi Yu](fieldroom/licenses/YuNet-MIT.txt) | 232,589 bytes |
| [YOLOX-s 2022nov](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/object_detection_yolox) | Person location and confidence | [Apache-2.0](fieldroom/licenses/YOLOX-Apache-2.0.txt) | 35,858,002 bytes |

The full upstream license texts ship with Fieldroom. Download URLs and SHA-256 checksums are recorded in `fieldroom/models.py`. Model files are downloaded during setup instead of being committed to this repository. The implementation follows the model preprocessing and output formats documented by OpenCV Zoo. YuNet is used only to locate faces; Fieldroom does not recognize anyone's identity.

### Offline AI detail models

| Model | Purpose in Fieldroom | License | Size |
| --- | --- | --- | ---: |
| [DnCNN color-blind, KAIR](https://github.com/cszn/KAIR), [ONNX conversion from npuscale v1.2](https://github.com/ikeno-web/npuscale/releases/tag/v1.2) | Neural color-image denoising | [Model: MIT](fieldroom/licenses/DnCNN-KAIR-MIT.txt); [conversion: MIT](fieldroom/licenses/DnCNN-ONNX-Conversion-MIT.txt) | 2,680,354 bytes |
| [ESPCN / sub-pixel CNN, ONNX Model Zoo](https://github.com/onnx/models/tree/c5cae0f1942c8d7727372c7b1f6b485aa39e4421/validated/vision/super_resolution/sub_pixel_cnn_2016) | Learned luminance detail blended at the original dimensions | [Apache-2.0](fieldroom/licenses/ESPCN-ONNX-Apache-2.0.txt) | 240,078 bytes |

The two detail files total 2,920,432 bytes; all four installed models total 39,011,023 bytes. Pinned download URLs and SHA-256 checksums are recorded in `fieldroom/enhance_models.py`. [AI detail notices](fieldroom/licenses/AI-Detail-NOTICE.txt) explain the upstream sources, conversion, and Fieldroom's adaptation. Model lookup and inference use only the verified local files.

### Layout and sample photographs

Layout A was selected in the [original design review](design-review/Fieldroom-Develop-Overview.jpg). The [interactive design picker](design-review/Fieldroom-Design-Picker.html) also remains available.

Dahlia is credited to Vultilion and the lakeside pavilion to Daniel Buechele. The bundled samples are resized JPEGs under Creative Commons Attribution 2.0. [Dahlia original](https://www.flickr.com/photos/vultilion/6056698931/) · [Pavilion original](https://www.flickr.com/photos/danielbuechele/6061409035/) · [License](https://creativecommons.org/licenses/by/2.0/)

Chelsea is by Stefan van der Walt, the espresso photograph by Rachel Michetti, and the photographer portrait by Lav Varshney. All three are CC0. The DSCOVR launch photograph is public domain, courtesy of SpaceX. [Sample image credits](https://scikit-image.org/docs/stable/api/skimage.data.html)
